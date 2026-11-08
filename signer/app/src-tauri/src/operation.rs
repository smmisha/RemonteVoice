//! Conservative install-effect journal. A timeout drops our future, not a device-side effect.
//! An unresolved gate survives process restarts and blocks another install for that device.
use crate::error::AppError;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    future::Future,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

static EFFECT_SEQUENCE: AtomicU64 = AtomicU64::new(0);
pub const INSTALL_DEADLINE: Duration = Duration::from_secs(600);

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Started,
    Unknown,
    Succeeded,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Receipt {
    schema: u8,
    id: String,
    artifact_sha256: String,
    target_device: String,
    bundle_id: String,
    phase: Phase,
    started_unix_ms: u128,
}

fn journal_error() -> AppError {
    AppError::new(
        "operation_journal",
        "Журнал установки недоступен. Установка остановлена; повтор требует проверки предыдущего результата.",
    )
}

pub fn reconciliation_required() -> AppError {
    AppError::new(
        "install_reconciliation_required",
        "Предыдущая установка на этот iPhone не подтверждена. Автоповтор заблокирован до проверки результата; перезапуск программы не снимает блокировку.",
    )
}

fn gate_path(directory: &Path, target: &str) -> PathBuf {
    directory.join(format!(
        "device-{:x}.pending.json",
        Sha256::digest(target.as_bytes())
    ))
}

struct DeviceLease {
    _file: File,
}

impl DeviceLease {
    fn acquire(directory: &Path, target: &str) -> Result<Self, AppError> {
        let path = directory.join(format!(
            "device-{:x}.lease",
            Sha256::digest(target.as_bytes())
        ));
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            options.share_mode(0); // OS releases this exclusive handle when the process exits.
        }
        let file = options.open(path).map_err(|_| AppError::new(
            "install_effect_active", "Эта установка ещё выполняется в другом процессе или журнал занят. Дождитесь завершения и обновите состояние.",
        ))?;
        Ok(Self { _file: file })
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PendingInstall {
    pub effect_id: String,
    pub artifact_sha256: String,
    pub target_device: String,
    pub bundle_id: String,
    pub phase: &'static str,
}

#[derive(Deserialize, Serialize)]
struct UserResolution {
    schema: u8,
    effect: Receipt,
    user_resolution: String,
    resolved_unix_ms: u128,
}

fn read_receipt(path: &Path) -> Result<Receipt, AppError> {
    let bytes = read_small(path)?;
    if bytes.len() > 16_384 {
        return Err(journal_error());
    }
    let receipt: Receipt = serde_json::from_slice(&bytes).map_err(|_| journal_error())?;
    if receipt.schema != 1
        || receipt.id.is_empty()
        || receipt.id.len() > 128
        || !receipt
            .id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        || receipt.artifact_sha256.len() != 64
        || !receipt
            .artifact_sha256
            .bytes()
            .all(|b| b.is_ascii_hexdigit())
        || receipt.target_device.is_empty()
        || receipt.target_device.len() > 256
        || receipt.bundle_id.len() > 1024
    {
        return Err(journal_error());
    }
    Ok(receipt)
}

fn read_small(path: &Path) -> Result<Vec<u8>, AppError> {
    let file = File::open(path).map_err(|_| journal_error())?;
    let mut bytes = Vec::new();
    file.take(16_385)
        .read_to_end(&mut bytes)
        .map_err(|_| journal_error())?;
    if bytes.len() > 16_384 {
        return Err(journal_error());
    }
    Ok(bytes)
}

pub fn pending(directory: &Path, target: &str) -> Result<Option<PendingInstall>, AppError> {
    let path = gate_path(directory, target);
    match std::fs::symlink_metadata(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(journal_error()),
        Ok(metadata) if !metadata.is_file() || metadata.file_type().is_symlink() => {
            return Err(journal_error());
        }
        Ok(_) => {}
    }
    let receipt = read_receipt(&path)?;
    if receipt.target_device != target {
        return Err(journal_error());
    }
    let phase = if directory
        .join(format!("{}.unknown.json", receipt.id))
        .is_file()
    {
        "unknown"
    } else if directory
        .join(format!("{}.succeeded.json", receipt.id))
        .is_file()
    {
        "success_recorded_pending_release"
    } else {
        "started_or_interrupted"
    };
    Ok(Some(PendingInstall {
        effect_id: receipt.id,
        artifact_sha256: receipt.artifact_sha256,
        target_device: receipt.target_device,
        bundle_id: receipt.bundle_id,
        phase,
    }))
}

/// Explicit human decision to permit a later, separately requested attempt. This never installs
/// or infers that the previous IPA was successfully installed from a version/bundle match.
pub fn resolve_after_manual_check(
    directory: &Path,
    target: &str,
    effect_id: &str,
) -> Result<(), AppError> {
    let _lease = DeviceLease::acquire(directory, target)?;
    let gate = gate_path(directory, target);
    let mut receipt = read_receipt(&gate)?;
    if receipt.target_device != target || receipt.id != effect_id {
        return Err(AppError::new(
            "install_effect_changed",
            "Выбрана другая операция. Обновите состояние и проверьте текущую установку.",
        ));
    }
    // Manual authorization settles an unconfirmed effect; it does not certify success even
    // when a library success receipt exists but durable gate cleanup was interrupted.
    receipt.phase = Phase::Unknown;
    let resolution_path = directory.join(format!("{}.user-resolved.json", receipt.id));
    let decision = "retry_authorized_after_manual_check";
    let resolution = UserResolution {
        schema: 1,
        effect: receipt.clone(),
        user_resolution: decision.into(),
        resolved_unix_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| journal_error())?
            .as_millis(),
    };
    let bytes = serde_json::to_vec(&resolution).map_err(|_| journal_error())?;
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&resolution_path)
    {
        Ok(mut file) => {
            file.write_all(&bytes).map_err(|_| journal_error())?;
            file.sync_all().map_err(|_| journal_error())?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            // A crash after fsync but before gate release can resume the same explicit decision.
            let existing = read_small(&resolution_path)?;
            if existing.len() > 16_384 {
                return Err(journal_error());
            }
            let previous: UserResolution =
                serde_json::from_slice(&existing).map_err(|_| journal_error())?;
            if previous.schema != 1
                || previous.effect.id != receipt.id
                || previous.effect.target_device != target
                || previous.effect.artifact_sha256 != receipt.artifact_sha256
                || previous.user_resolution != decision
            {
                return Err(journal_error());
            }
        }
        Err(_) => return Err(journal_error()),
    }
    // Single directory-entry removal is the release point. The lease excludes begin/resolve races
    // on Windows; a crash before removal leaves the exact old effect blocking, after removal its
    // durable user-resolution archive remains. No install call is made by this function.
    std::fs::remove_file(gate).map_err(|_| journal_error())?;
    Ok(())
}

/// Only receipt metadata is persisted: no account, password, token, path, or dependency error.
pub struct InstallEffect {
    directory: PathBuf,
    gate: PathBuf,
    receipt: Receipt,
    _lease: DeviceLease,
}

fn write_new(path: &Path, receipt: &Receipt) -> Result<(), AppError> {
    let bytes = serde_json::to_vec(receipt).map_err(|_| journal_error())?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| journal_error())?;
    file.write_all(&bytes).map_err(|_| journal_error())?;
    file.sync_all().map_err(|_| journal_error())?;
    Ok(())
}

impl InstallEffect {
    pub fn ensure_available(directory: &Path, target: &str) -> Result<(), AppError> {
        match std::fs::symlink_metadata(gate_path(directory, target)) {
            Ok(_) => Err(reconciliation_required()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(journal_error()),
        }
    }

    pub fn begin(
        directory: &Path,
        artifact_sha256: &str,
        target_device: &str,
        bundle_id: &str,
    ) -> Result<Self, AppError> {
        if artifact_sha256.len() != 64
            || !artifact_sha256.bytes().all(|b| b.is_ascii_hexdigit())
            || target_device.is_empty()
            || target_device.len() > 256
            || bundle_id.len() > 1024
        {
            return Err(journal_error());
        }
        std::fs::create_dir_all(directory).map_err(|_| journal_error())?;
        if std::fs::symlink_metadata(directory)
            .map_err(|_| journal_error())?
            .file_type()
            .is_symlink()
        {
            return Err(journal_error());
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| journal_error())?;
        let receipt = Receipt {
            schema: 1,
            id: format!(
                "{}-{}-{}",
                std::process::id(),
                now.as_nanos(),
                EFFECT_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ),
            artifact_sha256: artifact_sha256.to_string(),
            target_device: target_device.to_string(),
            bundle_id: bundle_id.to_string(),
            phase: Phase::Started,
            started_unix_ms: now.as_millis(),
        };
        let gate = gate_path(directory, target_device);
        let lease = DeviceLease::acquire(directory, target_device)?;
        // Exclusive creation is also the cross-process gate; check-then-write alone is insufficient.
        let mut file = match OpenOptions::new().write(true).create_new(true).open(&gate) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                return Err(reconciliation_required());
            }
            Err(_) => return Err(journal_error()),
        };
        let bytes = serde_json::to_vec(&receipt).map_err(|_| journal_error())?;
        file.write_all(&bytes).map_err(|_| journal_error())?;
        file.sync_all().map_err(|_| journal_error())?;
        // A partial/unreadable gate is intentionally still blocking. Never assume no effect after restart.
        Ok(Self {
            directory: directory.to_path_buf(),
            gate,
            receipt,
            _lease: lease,
        })
    }

    fn record(&self, phase: Phase) -> Result<(), AppError> {
        let mut receipt = self.receipt.clone();
        receipt.phase = phase;
        let suffix = match receipt.phase {
            Phase::Started => "started",
            Phase::Unknown => "unknown",
            Phase::Succeeded => "succeeded",
        };
        write_new(
            &self
                .directory
                .join(format!("{}.{}.json", receipt.id, suffix)),
            &receipt,
        )
    }

    pub async fn run<F, T>(self, deadline: Duration, work: F) -> Result<T, AppError>
    where
        F: Future<Output = Result<T, AppError>>,
    {
        match tokio::time::timeout(deadline, work).await {
            Ok(Ok(value)) => {
                // Persist success before releasing the gate. Failure to persist remains fail-closed.
                self.record(Phase::Succeeded)?;
                std::fs::remove_file(&self.gate).map_err(|_| journal_error())?;
                Ok(value)
            }
            Ok(Err(_)) => {
                self.record(Phase::Unknown)?;
                Err(AppError::new(
                    "install_unconfirmed",
                    "Результат установки не подтверждён. Повтор заблокирован до проверки приложения на iPhone.",
                ))
            }
            Err(_) => {
                self.record(Phase::Unknown)?;
                Err(AppError::new(
                    "install_timeout_unknown",
                    "Истекло время ожидания установки. iPhone мог продолжить операцию; повтор заблокирован до проверки результата.",
                ))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let path = std::env::temp_dir().join(format!(
                "localsign-operation-test-{}-{now}-{}",
                std::process::id(),
                EFFECT_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn begin(&self) -> InstallEffect {
            InstallEffect::begin(
                &self.0,
                &"a".repeat(64),
                "fixture-device",
                "dev.fixture.app",
            )
            .unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            assert!(self.0.starts_with(std::env::temp_dir()));
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn interrupted_effect_blocks_retry_after_restart() {
        let fixture = Fixture::new();
        drop(fixture.begin()); // Process died after durable start, before a terminal result.
        assert_eq!(
            InstallEffect::ensure_available(&fixture.0, "fixture-device")
                .unwrap_err()
                .code,
            "install_reconciliation_required"
        );
        assert_eq!(
            InstallEffect::begin(
                &fixture.0,
                &"b".repeat(64),
                "fixture-device",
                "different.app"
            )
            .err()
            .unwrap()
            .code,
            "install_reconciliation_required"
        );
        assert!(InstallEffect::ensure_available(&fixture.0, "different-device").is_ok());
    }

    #[test]
    fn successful_fake_install_persists_receipt_and_allows_retry() {
        let fixture = Fixture::new();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        assert_eq!(
            runtime
                .block_on(fixture.begin().run(Duration::from_secs(1), async { Ok(7) }))
                .unwrap(),
            7
        );
        assert!(InstallEffect::ensure_available(&fixture.0, "fixture-device").is_ok());
        let receipt = std::fs::read_dir(&fixture.0)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| path.extension().is_some_and(|ext| ext == "json"))
            .unwrap();
        let value: Receipt = serde_json::from_slice(&std::fs::read(receipt).unwrap()).unwrap();
        assert!(matches!(value.phase, Phase::Succeeded));
        assert_eq!(value.artifact_sha256, "a".repeat(64));
        assert_eq!(value.target_device, "fixture-device");
    }

    #[test]
    fn pending_fake_install_times_out_releases_busy_and_remains_blocked() {
        let fixture = Fixture::new();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let busy = tokio::sync::Mutex::new(());
            let result = async {
                let _busy = busy.try_lock().unwrap();
                fixture
                    .begin()
                    .run(
                        Duration::from_millis(5),
                        std::future::pending::<Result<(), AppError>>(),
                    )
                    .await
            }
            .await;
            assert_eq!(result.unwrap_err().code, "install_timeout_unknown");
            assert!(busy.try_lock().is_ok());
        });
        assert!(InstallEffect::ensure_available(&fixture.0, "fixture-device").is_err());
        let phases: Vec<String> = std::fs::read_dir(&fixture.0)
            .unwrap()
            .map(|entry| std::fs::read_to_string(entry.unwrap().path()).unwrap())
            .collect();
        assert!(
            phases
                .iter()
                .any(|value| value.contains("\"phase\":\"unknown\""))
        );
    }

    #[test]
    fn dependency_error_content_never_enters_receipts() {
        let fixture = Fixture::new();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let result: Result<(), AppError> =
            runtime.block_on(fixture.begin().run(Duration::from_secs(1), async {
                Err(AppError::new("fixture", "fixture-password-never-persist"))
            }));
        assert_eq!(result.unwrap_err().code, "install_unconfirmed");
        for entry in std::fs::read_dir(&fixture.0).unwrap() {
            assert!(
                !std::fs::read_to_string(entry.unwrap().path())
                    .unwrap()
                    .contains("fixture-password-never-persist")
            );
        }
    }

    #[test]
    fn existing_partial_gate_is_never_treated_as_safe_to_retry() {
        let fixture = Fixture::new();
        std::fs::write(gate_path(&fixture.0, "fixture-device"), b"{partial").unwrap();
        assert!(InstallEffect::ensure_available(&fixture.0, "fixture-device").is_err());
        assert!(
            InstallEffect::begin(
                &fixture.0,
                &"a".repeat(64),
                "fixture-device",
                "dev.fixture.app"
            )
            .is_err()
        );
    }

    #[test]
    fn stale_id_or_wrong_device_cannot_release_current_effect() {
        let fixture = Fixture::new();
        let effect = fixture.begin();
        let id = effect.receipt.id.clone();
        drop(effect);
        assert!(resolve_after_manual_check(&fixture.0, "fixture-device", "stale-id").is_err());
        assert!(resolve_after_manual_check(&fixture.0, "wrong-device", &id).is_err());
        assert_eq!(
            pending(&fixture.0, "fixture-device")
                .unwrap()
                .unwrap()
                .effect_id,
            id
        );
        assert!(InstallEffect::ensure_available(&fixture.0, "fixture-device").is_err());
    }

    #[test]
    fn manual_resolution_is_durable_and_never_starts_a_retry() {
        let fixture = Fixture::new();
        let effect = fixture.begin();
        let id = effect.receipt.id.clone();
        drop(effect);
        resolve_after_manual_check(&fixture.0, "fixture-device", &id).unwrap();
        assert!(pending(&fixture.0, "fixture-device").unwrap().is_none());
        let archive = fixture.0.join(format!("{id}.user-resolved.json"));
        let saved: UserResolution =
            serde_json::from_slice(&std::fs::read(archive).unwrap()).unwrap();
        assert_eq!(saved.effect.id, id);
        assert_eq!(saved.effect.target_device, "fixture-device");
        assert_eq!(saved.effect.artifact_sha256, "a".repeat(64));
        assert!(matches!(saved.effect.phase, Phase::Unknown));
        assert_eq!(saved.user_resolution, "retry_authorized_after_manual_check");
        assert!(!fixture.0.join(format!("{id}.succeeded.json")).exists());
        assert_eq!(
            std::fs::read_dir(&fixture.0)
                .unwrap()
                .filter(|entry| entry
                    .as_ref()
                    .unwrap()
                    .path()
                    .extension()
                    .is_some_and(|ext| ext == "json"))
                .count(),
            1
        );
        // Only this separate explicit begin creates a later operation. Resolution created none.
        let later = fixture.begin();
        assert_ne!(later.receipt.id, id);
    }

    #[test]
    fn recorded_manual_decision_can_finish_gate_release_after_interruption() {
        let fixture = Fixture::new();
        let effect = fixture.begin();
        let id = effect.receipt.id.clone();
        let gate = effect.gate.clone();
        let started = std::fs::read(&gate).unwrap();
        drop(effect);
        resolve_after_manual_check(&fixture.0, "fixture-device", &id).unwrap();
        let resolution = fixture.0.join(format!("{id}.user-resolved.json"));
        let archived = std::fs::read(&resolution).unwrap();
        std::fs::write(&gate, started).unwrap(); // State after fsync before gate deletion.
        resolve_after_manual_check(&fixture.0, "fixture-device", &id).unwrap();
        assert!(!gate.exists());
        assert_eq!(std::fs::read(resolution).unwrap(), archived);
    }

    #[cfg(windows)]
    #[test]
    fn active_install_lease_prevents_manual_release_from_another_process_path() {
        let fixture = Fixture::new();
        let effect = fixture.begin();
        let id = effect.receipt.id.clone();
        assert_eq!(
            resolve_after_manual_check(&fixture.0, "fixture-device", &id)
                .unwrap_err()
                .code,
            "install_effect_active"
        );
        assert!(effect.gate.exists());
        drop(effect);
        resolve_after_manual_check(&fixture.0, "fixture-device", &id).unwrap();
    }

    #[test]
    fn failed_success_receipt_write_keeps_gate_and_allows_explicit_manual_recovery() {
        let fixture = Fixture::new();
        let effect = fixture.begin();
        let id = effect.receipt.id.clone();
        std::fs::create_dir(fixture.0.join(format!("{id}.succeeded.json"))).unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let result = runtime.block_on(effect.run(Duration::from_secs(1), async { Ok(()) }));
        assert_eq!(result.unwrap_err().code, "operation_journal");
        assert!(InstallEffect::ensure_available(&fixture.0, "fixture-device").is_err());
        resolve_after_manual_check(&fixture.0, "fixture-device", &id).unwrap();
        assert!(InstallEffect::ensure_available(&fixture.0, "fixture-device").is_ok());
    }
}
