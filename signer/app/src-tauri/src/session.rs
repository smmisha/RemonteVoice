//! Optional persisted Apple Account session for automatic renewal.
//!
//! Only the technical session material (SPD and its Apple Account id) is stored.
//! The password is never stored. Secrets live in Windows Credential Manager under
//! the iReSign service namespace, split into bounded chunks because Credential
//! Manager rejects large generic-credential blobs. There is no plaintext fallback.
use crate::error::AppError;
use crate::storage::SERVICE;
use plist::{Dictionary, Value};
use serde::{Deserialize, Serialize};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::Emitter;

const META_KEY: &str = "session.meta";
const BANKS: [&str; 2] = ["a", "b"];
const SELFTEST_SERVICE: &str = "dev.remontevoice.iresign.v1.selftest";
const MAX_CHUNKS: usize = 24;
const CHUNK_DATA_BYTES: usize = 1400;
const MAX_RECORD_BYTES: usize = MAX_CHUNKS * CHUNK_DATA_BYTES;
const GENERATION_BYTES: usize = 16;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionState {
    pub status: &'static str,
    pub email: Option<String>,
    pub stored: bool,
}

#[derive(Clone)]
pub struct SessionView {
    pub status: &'static str,
    pub email: Option<String>,
    pub stored: bool,
}

impl Default for SessionView {
    fn default() -> Self {
        Self {
            status: "absent",
            email: None,
            stored: false,
        }
    }
}

impl SessionView {
    pub fn state(&self) -> SessionState {
        SessionState {
            status: self.status,
            email: self.email.clone(),
            stored: self.stored,
        }
    }
}

pub struct SessionRecord {
    pub email: String,
    pub anisette_url: String,
    pub adsid: String,
    pub spd: Dictionary,
}

fn session_storage() -> AppError {
    AppError::new(
        "session_storage",
        "Не удалось сохранить или прочитать сессию в защищённом хранилище Windows.",
    )
}

fn session_corrupt() -> AppError {
    AppError::new(
        "session_corrupt",
        "Сохранённая сессия повреждена. Войдите снова или забудьте её.",
    )
}

pub fn session_restore() -> AppError {
    AppError::new(
        "session_restore",
        "Сохранённую сессию не удалось восстановить. Войдите снова.",
    )
}

pub trait SecretStore: Send + Sync {
    fn set(&self, key: &str, value: &[u8]) -> Result<(), AppError>;
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, AppError>;
    fn delete(&self, key: &str) -> Result<(), AppError>;
}

pub struct WindowsStore;

struct KeyringStore {
    service: &'static str,
}

impl SecretStore for KeyringStore {
    fn set(&self, key: &str, value: &[u8]) -> Result<(), AppError> {
        keyring::Entry::new(self.service, key)
            .map_err(|_| session_storage())?
            .set_secret(value)
            .map_err(|_| session_storage())
    }

    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, AppError> {
        let entry = keyring::Entry::new(self.service, key).map_err(|_| session_storage())?;
        match entry.get_secret() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err(session_storage()),
        }
    }

    fn delete(&self, key: &str) -> Result<(), AppError> {
        let entry = keyring::Entry::new(self.service, key).map_err(|_| session_storage())?;
        match entry.delete_credential() {
            Ok(()) => Ok(()),
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(session_storage()),
        }
    }
}

impl SecretStore for WindowsStore {
    fn set(&self, key: &str, value: &[u8]) -> Result<(), AppError> {
        KeyringStore { service: SERVICE }.set(key, value)
    }

    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, AppError> {
        KeyringStore { service: SERVICE }.get(key)
    }

    fn delete(&self, key: &str) -> Result<(), AppError> {
        KeyringStore { service: SERVICE }.delete(key)
    }
}

#[derive(Serialize, Deserialize)]
struct Meta {
    schema: u8,
    generation: String,
    chunks: u32,
    bank: String,
    saved_at_ms: u64,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis() as u64)
        .unwrap_or(0)
}

fn generation() -> String {
    let value = now_ms().wrapping_mul(1_000_003) ^ u64::from(std::process::id());
    format!("{value:016x}")
}

fn valid_generation(value: &str) -> bool {
    value.len() == GENERATION_BYTES && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn valid_email(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 320
        && !value.chars().any(|c| c.is_control())
        && value.contains('@')
}

fn valid_url(value: &str) -> bool {
    let Ok(url) = url::Url::parse(value) else {
        return false;
    };
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
}

fn valid_spd(spd: &Dictionary, adsid: &str) -> bool {
    spd.get("adsid").and_then(Value::as_string) == Some(adsid)
        && spd
            .get("GsIdmsToken")
            .and_then(Value::as_string)
            .is_some_and(|value| !value.is_empty())
        && spd
            .get("sk")
            .and_then(Value::as_data)
            .is_some_and(|value| !value.is_empty())
        && spd
            .get("c")
            .and_then(Value::as_data)
            .is_some_and(|value| !value.is_empty())
}

fn encode(record: &SessionRecord) -> Result<Vec<u8>, AppError> {
    if !valid_email(&record.email)
        || !valid_url(&record.anisette_url)
        || record.adsid.is_empty()
        || record.adsid.len() > 128
        || !valid_spd(&record.spd, &record.adsid)
    {
        return Err(session_storage());
    }
    let mut dictionary = Dictionary::new();
    dictionary.insert("schema".into(), Value::from(1u64));
    dictionary.insert("email".into(), Value::from(record.email.clone()));
    dictionary.insert(
        "anisetteUrl".into(),
        Value::from(record.anisette_url.clone()),
    );
    dictionary.insert("adsid".into(), Value::from(record.adsid.clone()));
    dictionary.insert("spd".into(), Value::Dictionary(record.spd.clone()));
    dictionary.insert("savedAtMs".into(), Value::from(now_ms()));
    let mut bytes = Vec::new();
    plist::to_writer_binary(&mut bytes, &Value::Dictionary(dictionary))
        .map_err(|_| session_storage())?;
    if bytes.is_empty() || bytes.len() > MAX_RECORD_BYTES {
        return Err(session_storage());
    }
    Ok(bytes)
}

fn decode(bytes: &[u8]) -> Result<SessionRecord, AppError> {
    let value =
        plist::Value::from_reader(std::io::Cursor::new(bytes)).map_err(|_| session_corrupt())?;
    let dictionary = value.as_dictionary().ok_or_else(session_corrupt)?;
    if dictionary
        .get("schema")
        .and_then(Value::as_unsigned_integer)
        != Some(1)
    {
        return Err(session_corrupt());
    }
    let text = |key: &str| {
        dictionary
            .get(key)
            .and_then(Value::as_string)
            .map(str::to_string)
    };
    let email = text("email").ok_or_else(session_corrupt)?;
    let anisette_url = text("anisetteUrl").ok_or_else(session_corrupt)?;
    let adsid = text("adsid").ok_or_else(session_corrupt)?;
    let spd = dictionary
        .get("spd")
        .and_then(Value::as_dictionary)
        .cloned()
        .ok_or_else(session_corrupt)?;
    if !valid_email(&email)
        || !valid_url(&anisette_url)
        || adsid.is_empty()
        || adsid.len() > 128
        || !valid_spd(&spd, &adsid)
    {
        return Err(session_corrupt());
    }
    Ok(SessionRecord {
        email,
        anisette_url,
        adsid,
        spd,
    })
}

fn read_meta(store: &dyn SecretStore) -> Result<Option<Meta>, AppError> {
    let Some(bytes) = store.get(META_KEY)? else {
        return Ok(None);
    };
    if bytes.len() > 4096 {
        return Err(session_corrupt());
    }
    let meta: Meta = serde_json::from_slice(&bytes).map_err(|_| session_corrupt())?;
    if meta.schema != 1
        || !valid_generation(&meta.generation)
        || meta.chunks == 0
        || meta.chunks as usize > MAX_CHUNKS
        || !BANKS.contains(&meta.bank.as_str())
    {
        return Err(session_corrupt());
    }
    Ok(Some(meta))
}

fn chunk_key(bank: &str, index: usize) -> String {
    format!("session.{bank}.chunk.{index}")
}

pub fn save(store: &dyn SecretStore, record: &SessionRecord) -> Result<(), AppError> {
    let bytes = encode(record)?;
    let generation = generation();
    let chunks: Vec<&[u8]> = bytes.chunks(CHUNK_DATA_BYTES).collect();
    if chunks.is_empty() || chunks.len() > MAX_CHUNKS {
        return Err(session_storage());
    }
    // A damaged commit point must not prevent replacing the record.
    let previous = read_meta(store).unwrap_or_default();
    let bank = match previous.as_ref().map(|meta| meta.bank.as_str()) {
        Some("a") => "b",
        _ => "a",
    };
    for (index, chunk) in chunks.iter().enumerate() {
        let mut value = Vec::with_capacity(GENERATION_BYTES + chunk.len());
        value.extend_from_slice(generation.as_bytes());
        value.extend_from_slice(chunk);
        store.set(&chunk_key(bank, index), &value)?;
    }
    // Meta is the commit point. The previous bank is untouched until this write
    // succeeds, so a partial save leaves the last good session loadable.
    let meta = Meta {
        schema: 1,
        generation,
        chunks: chunks.len() as u32,
        bank: bank.to_string(),
        saved_at_ms: now_ms(),
    };
    let meta_bytes = serde_json::to_vec(&meta).map_err(|_| session_storage())?;
    store.set(META_KEY, &meta_bytes)?;
    if let Some(previous) = previous {
        for index in 0..MAX_CHUNKS {
            let _ = store.delete(&chunk_key(&previous.bank, index));
        }
    }
    Ok(())
}

pub fn load(store: &dyn SecretStore) -> Result<Option<SessionRecord>, AppError> {
    let Some(meta) = read_meta(store)? else {
        return Ok(None);
    };
    let mut bytes = Vec::new();
    for index in 0..meta.chunks as usize {
        let value = store
            .get(&chunk_key(&meta.bank, index))?
            .ok_or_else(session_corrupt)?;
        if value.len() < GENERATION_BYTES
            || value.get(..GENERATION_BYTES) != Some(meta.generation.as_bytes())
        {
            return Err(session_corrupt());
        }
        bytes.extend_from_slice(&value[GENERATION_BYTES..]);
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(session_corrupt());
        }
    }
    decode(&bytes).map(Some)
}

pub fn exists(store: &dyn SecretStore) -> Result<bool, AppError> {
    Ok(store.get(META_KEY)?.is_some())
}

pub fn clear(store: &dyn SecretStore) -> Result<(), AppError> {
    // Delete both banks and the commit point. A successful clear leaves no
    // unknown chunk slots, including an orphan from an interrupted save.
    let mut first_error = None;
    for bank in BANKS {
        for index in 0..MAX_CHUNKS {
            if let Err(error) = store.delete(&chunk_key(bank, index))
                && first_error.is_none()
            {
                first_error = Some(error);
            }
        }
    }
    if let Err(error) = store.delete(META_KEY)
        && first_error.is_none()
    {
        first_error = Some(error);
    }
    match first_error {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

pub async fn load_record_async() -> Result<Option<SessionRecord>, AppError> {
    tauri::async_runtime::spawn_blocking(|| load(&WindowsStore))
        .await
        .map_err(|_| session_storage())?
}

pub async fn save_record_async(
    email: String,
    anisette_url: String,
    spd: Dictionary,
) -> Result<(), AppError> {
    tauri::async_runtime::spawn_blocking(move || {
        let adsid = spd
            .get("adsid")
            .and_then(Value::as_string)
            .filter(|value| !value.is_empty())
            .ok_or_else(session_storage)?
            .to_string();
        save(
            &WindowsStore,
            &SessionRecord {
                email,
                anisette_url,
                adsid,
                spd,
            },
        )
    })
    .await
    .map_err(|_| session_storage())?
}

pub async fn delete_record_async() -> Result<(), AppError> {
    tauri::async_runtime::spawn_blocking(|| clear(&WindowsStore))
        .await
        .map_err(|_| session_storage())?
}

pub async fn stored_async() -> Result<bool, AppError> {
    tauri::async_runtime::spawn_blocking(|| exists(&WindowsStore))
        .await
        .map_err(|_| session_storage())?
}

/// Local diagnostic for the real Windows Credential Manager: saves a fixed
/// non-Apple fixture, reads it back, then removes every chunk. It never contains
/// a real account token and is not part of the normal signing flow.
pub fn self_test() -> Result<(), AppError> {
    let mut spd = Dictionary::new();
    spd.insert("adsid".into(), Value::from("iresign-storage-fixture"));
    spd.insert("GsIdmsToken".into(), Value::from("fixture-not-a-token"));
    spd.insert("sk".into(), Value::Data(vec![0x5a; 6000]));
    spd.insert("c".into(), Value::Data(vec![0x33; 16]));
    let record = SessionRecord {
        email: "iresign-fixture@example.invalid".into(),
        anisette_url: "https://example.invalid".into(),
        adsid: "iresign-storage-fixture".into(),
        spd,
    };
    let store = KeyringStore {
        service: SELFTEST_SERVICE,
    };
    let outcome = (|| {
        save(&store, &record)?;
        let loaded = load(&store)?.ok_or_else(session_storage)?;
        if loaded.email != record.email
            || loaded.anisette_url != record.anisette_url
            || loaded.adsid != record.adsid
            || loaded.spd != record.spd
        {
            return Err(session_storage());
        }
        Ok(())
    })();
    let cleared = clear(&store);
    outcome?;
    cleared?;
    if exists(&store)? {
        return Err(session_storage());
    }
    Ok(())
}

/// Rebuild a signing session without the password. Apple's app-token endpoint is
/// called with the stored SPD; if Apple invalidated it, the fixed restore error is
/// returned and the user must sign in again.
pub async fn restore_signer(record: SessionRecord) -> Result<crate::Signer, AppError> {
    use isideload::{
        anisette::remote_v3::RemoteV3AnisetteProvider,
        auth::apple_account::AppleAccount,
        dev::developer_session::DeveloperSession,
        sideload::{SideloaderBuilder, builder::MaxCertsBehavior},
        util::callbacks::MaxCertsCallbackBox,
    };

    let work = async {
        let provider = RemoteV3AnisetteProvider::new(
            &record.anisette_url,
            crate::storage::protected_storage()?,
            "0".to_string(),
        )
        .map_err(|_| session_restore())?;
        let mut account = AppleAccount::builder(&record.email)
            .anisette_provider(provider)
            .build()
            .await
            .map_err(|_| session_restore())?;
        account.spd = Some(record.spd);
        let token = account
            .get_app_token("xcode.auth")
            .await
            .map_err(|_| session_restore())?;
        let developer = DeveloperSession::new(
            token,
            record.adsid,
            account.grandslam_client.clone(),
            account.anisette_generator.clone(),
        );
        Ok::<_, AppError>(
            SideloaderBuilder::<MaxCertsCallbackBox>::new(developer, record.email.clone())
                .machine_name("iReSign".into())
                .storage(crate::storage::protected_storage()?)
                .max_certs_behavior(MaxCertsBehavior::Error)
                .build(),
        )
    };
    match tokio::time::timeout(Duration::from_secs(120), work).await {
        Ok(result) => result,
        Err(_) => Err(session_restore()),
    }
}

pub fn emit(app: &tauri::AppHandle, state: &crate::Runtime) {
    let value = state
        .session
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .state();
    let _ = app.emit("session-state", value);
}

/// The live signer was dropped after an uncertain/failed install. The account
/// session itself may still be restorable, but signing requires a new login.
pub fn mark_inactive(app: &tauri::AppHandle, state: &crate::Runtime) {
    {
        let mut view = state
            .session
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        view.status = "failed";
    }
    emit(app, state);
}

#[tauri::command]
pub fn session_state(state: tauri::State<'_, crate::Runtime>) -> SessionState {
    state
        .session
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .state()
}

#[tauri::command]
pub async fn forget_stored_session(
    app: tauri::AppHandle,
    state: tauri::State<'_, crate::Runtime>,
) -> Result<SessionState, AppError> {
    delete_record_async().await?;
    {
        let mut view = state
            .session
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        view.stored = false;
    }
    emit(&app, &state);
    Ok(state
        .session
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .state())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::HashMap, sync::Mutex};

    #[derive(Default)]
    struct MemoryStore {
        values: Mutex<HashMap<String, Vec<u8>>>,
    }

    impl SecretStore for MemoryStore {
        fn set(&self, key: &str, value: &[u8]) -> Result<(), AppError> {
            self.values
                .lock()
                .unwrap()
                .insert(key.to_string(), value.to_vec());
            Ok(())
        }

        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, AppError> {
            Ok(self.values.lock().unwrap().get(key).cloned())
        }

        fn delete(&self, key: &str) -> Result<(), AppError> {
            self.values.lock().unwrap().remove(key);
            Ok(())
        }
    }

    fn spd(adsid: &str) -> Dictionary {
        let mut value = Dictionary::new();
        value.insert("adsid".into(), Value::from(adsid));
        value.insert("GsIdmsToken".into(), Value::from("fixture-token"));
        value.insert("sk".into(), Value::Data(vec![7u8; 32]));
        value.insert("c".into(), Value::Data(vec![9u8; 16]));
        value
    }

    fn record() -> SessionRecord {
        SessionRecord {
            email: "fixture@example.com".into(),
            anisette_url: "https://anisette.example.com".into(),
            adsid: "fixture-adsid".into(),
            spd: spd("fixture-adsid"),
        }
    }

    #[test]
    fn session_roundtrip_uses_chunks_and_clear_removes_them() {
        let store = MemoryStore::default();
        let mut value = record();
        value
            .spd
            .insert("large".into(), Value::Data(vec![3u8; 6000]));
        save(&store, &value).unwrap();
        assert!(exists(&store).unwrap());
        let keys: Vec<String> = store.values.lock().unwrap().keys().cloned().collect();
        assert!(keys.iter().any(|key| key == "session.a.chunk.1"));
        let loaded = load(&store).unwrap().unwrap();
        assert_eq!(loaded.email, value.email);
        assert_eq!(loaded.anisette_url, value.anisette_url);
        assert_eq!(loaded.adsid, value.adsid);
        assert_eq!(loaded.spd, value.spd);
        clear(&store).unwrap();
        assert!(!exists(&store).unwrap());
        assert!(store.values.lock().unwrap().is_empty());
    }

    #[test]
    fn corrupt_or_missing_chunk_is_never_partial_session() {
        let store = MemoryStore::default();
        save(&store, &record()).unwrap();
        let meta = read_meta(&store).unwrap().unwrap();
        store
            .values
            .lock()
            .unwrap()
            .remove(&chunk_key(&meta.bank, 0));
        assert_eq!(load(&store).err().unwrap().code, "session_corrupt");

        let store = MemoryStore::default();
        store
            .values
            .lock()
            .unwrap()
            .insert(META_KEY.into(), b"not-json".to_vec());
        assert_eq!(load(&store).err().unwrap().code, "session_corrupt");
        assert!(exists(&store).unwrap());
    }

    #[test]
    fn stale_generation_chunk_is_rejected() {
        let store = MemoryStore::default();
        save(&store, &record()).unwrap();
        let meta = read_meta(&store).unwrap().unwrap();
        let key = chunk_key(&meta.bank, 0);
        let mut chunk = store.values.lock().unwrap().get(&key).cloned().unwrap();
        chunk[..GENERATION_BYTES].copy_from_slice(b"0000000000000000");
        store.values.lock().unwrap().insert(key, chunk);
        assert_eq!(load(&store).err().unwrap().code, "session_corrupt");
        assert!(valid_generation(&meta.generation));
    }

    #[test]
    fn second_save_switches_bank_and_removes_the_old_one() {
        let store = MemoryStore::default();
        save(&store, &record()).unwrap();
        let first = read_meta(&store).unwrap().unwrap();
        let mut updated = record();
        updated.email = "second@example.com".into();
        save(&store, &updated).unwrap();
        let second = read_meta(&store).unwrap().unwrap();
        assert_ne!(first.bank, second.bank);
        assert_eq!(load(&store).unwrap().unwrap().email, "second@example.com");
        let keys: Vec<String> = store.values.lock().unwrap().keys().cloned().collect();
        assert!(
            keys.iter()
                .all(|key| !key.starts_with(&format!("session.{}.", first.bank)))
        );
    }

    #[test]
    fn failed_save_keeps_previous_bank_loadable() {
        struct FailingStore {
            inner: MemoryStore,
            fail_key: String,
        }
        impl SecretStore for FailingStore {
            fn set(&self, key: &str, value: &[u8]) -> Result<(), AppError> {
                if key == self.fail_key {
                    return Err(session_storage());
                }
                self.inner.set(key, value)
            }
            fn get(&self, key: &str) -> Result<Option<Vec<u8>>, AppError> {
                self.inner.get(key)
            }
            fn delete(&self, key: &str) -> Result<(), AppError> {
                self.inner.delete(key)
            }
        }
        let inner = MemoryStore::default();
        save(&inner, &record()).unwrap();
        let first = read_meta(&inner).unwrap().unwrap();
        let mut updated = record();
        updated.email = "second@example.com".into();
        updated
            .spd
            .insert("large".into(), Value::Data(vec![1u8; 6000]));
        let failing = FailingStore {
            inner,
            fail_key: format!(
                "session.{}.chunk.1",
                if first.bank == "a" { "b" } else { "a" }
            ),
        };
        assert_eq!(
            save(&failing, &updated).unwrap_err().code,
            "session_storage"
        );
        // The commit point still points at the intact previous bank.
        let loaded = load(&failing).unwrap().unwrap();
        assert_eq!(loaded.email, "fixture@example.com");
    }

    #[test]
    fn rejects_invalid_or_oversized_session_material() {
        let store = MemoryStore::default();
        let mut value = record();
        value.anisette_url = "http://anisette.example.com".into();
        assert_eq!(save(&store, &value).unwrap_err().code, "session_storage");

        let mut value = record();
        value.spd.remove("adsid");
        assert_eq!(save(&store, &value).unwrap_err().code, "session_storage");

        let mut value = record();
        value
            .spd
            .insert("huge".into(), Value::Data(vec![0u8; MAX_RECORD_BYTES]));
        assert_eq!(save(&store, &value).unwrap_err().code, "session_storage");
        assert!(!exists(&store).unwrap());
    }

    #[test]
    fn no_session_metadata_means_absent() {
        let store = MemoryStore::default();
        assert!(load(&store).unwrap().is_none());
        assert!(!exists(&store).unwrap());
        clear(&store).unwrap();
    }
}
