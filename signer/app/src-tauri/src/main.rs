#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod device;
mod error;
mod ipa;
mod operation;
mod storage;

use error::AppError;
use isideload::{
    anisette::remote_v3::RemoteV3AnisetteProvider,
    auth::apple_account::{AppleAccount, TwoFactorCallbackResponse},
    dev::developer_session::DeveloperSession,
    sideload::{SideloaderBuilder, builder::MaxCertsBehavior, sideloader::Sideloader},
    util::callbacks::MaxCertsCallbackBox,
};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tauri::{Emitter, Manager, State};
use tauri_plugin_dialog::DialogExt;
use tokio::sync::{Mutex as AsyncMutex, oneshot};
use zeroize::Zeroizing;

type Signer = Sideloader<MaxCertsCallbackBox>;
type Challenge = Arc<Mutex<Option<oneshot::Sender<String>>>>;

#[derive(Default)]
struct Runtime {
    busy: AsyncMutex<()>,
    signer: AsyncMutex<Option<Signer>>,
    selected_device: Mutex<Option<String>>,
    selected_ipa: Mutex<Option<Arc<ipa::StagedIpa>>>,
    challenge: Challenge,
}

#[tauri::command]
fn protected_storage_available() -> bool {
    storage::protected_storage().is_ok()
}

#[tauri::command]
async fn list_devices() -> Result<Vec<device::DeviceInfo>, AppError> {
    device::list().await
}

fn data_directory(app: &tauri::AppHandle) -> Result<std::path::PathBuf, AppError> {
    app.path().app_local_data_dir().map_err(|_| {
        AppError::new(
            "application_storage",
            "Каталог данных приложения недоступен. Операция остановлена.",
        )
    })
}

#[tauri::command]
async fn select_device(state: State<'_, Runtime>, udid: String) -> Result<(), AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    if !device::list().await?.iter().any(|d| d.udid == udid) {
        return Err(AppError::new(
            "device_absent",
            "Выбранный iPhone отключён. Обновите список устройств.",
        ));
    }
    *state.selected_device.lock().map_err(|_| AppError::busy())? = Some(udid);
    Ok(())
}

#[tauri::command]
async fn choose_ipa(
    app: tauri::AppHandle,
    state: State<'_, Runtime>,
) -> Result<Option<ipa::IpaInfo>, AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let staging = data_directory(&app)?.join("ipa-staging");
    let path = tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .add_filter("iPhone application", &["ipa"])
            .blocking_pick_file()
    })
    .await
    .map_err(|_| AppError::new("file_dialog", "Не удалось открыть окно выбора IPA."))?;
    let Some(path) = path else {
        return Ok(None);
    };
    let path = path
        .into_path()
        .map_err(|_| AppError::new("ipa_path", "Выберите локальный IPA-файл."))?;
    let staged = tauri::async_runtime::spawn_blocking(move || ipa::stage(path, &staging))
        .await
        .map_err(|_| AppError::new("invalid_ipa", "Не удалось прочитать IPA."))??;
    let info = staged.info().clone();
    *state.selected_ipa.lock().map_err(|_| AppError::busy())? = Some(Arc::new(staged));
    Ok(Some(info))
}

fn anisette_url(value: &str) -> Result<String, AppError> {
    let error = || {
        AppError::new(
            "anisette_url",
            "Введите HTTPS-адрес своего Anisette-сервиса без пароля, query или fragment.",
        )
    };
    let url = url::Url::parse(value.trim()).map_err(|_| error())?;
    if url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(error());
    }
    Ok(url.as_str().trim_end_matches('/').to_string())
}

// Login flow adapted from iloader (MIT). No arbitrary tracing/log files are enabled.
#[tauri::command]
async fn login_new(
    app: tauri::AppHandle,
    state: State<'_, Runtime>,
    email: String,
    password: String,
    anisette_server: String,
) -> Result<(), AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let password = Zeroizing::new(password);
    let email = email.trim().to_lowercase();
    if email.is_empty() || password.is_empty() {
        return Err(AppError::new(
            "credentials",
            "Введите Apple Account и пароль.",
        ));
    }
    let url = anisette_url(&anisette_server)?;
    let provider =
        RemoteV3AnisetteProvider::new(&url, storage::protected_storage()?, "0".to_string())?;
    let challenge = state.challenge.clone();
    let challenge_callback = challenge.clone();
    let app_callback = app.clone();
    let callback = move |_params| {
        let app = app_callback.clone();
        let challenge = challenge_callback.clone();
        async move {
            let (tx, rx) = oneshot::channel();
            *challenge
                .lock()
                .map_err(|_| rootcause::report!("Authentication unavailable"))? = Some(tx);
            app.emit("two-factor-required", ())?;
            let code = tokio::time::timeout(Duration::from_secs(180), rx).await??;
            if code.is_empty() {
                return Err(rootcause::report!("Authentication canceled"));
            }
            Ok(TwoFactorCallbackResponse::SubmitCode(code))
        }
    };
    let login = tokio::time::timeout(
        Duration::from_secs(300),
        AppleAccount::builder(&email)
            .anisette_provider(provider)
            .login(&password, callback),
    )
    .await;
    // Always remove the sender, including timeout/error paths.
    challenge.lock().map_err(|_| AppError::busy())?.take();
    let _ = app.emit("two-factor-closed", ());
    let mut account = login.map_err(|_| {
        AppError::new(
            "login_timeout",
            "Вход не завершён за пять минут. Повторите позже.",
        )
    })??;
    let session = tokio::time::timeout(
        Duration::from_secs(90),
        DeveloperSession::from_account(&mut account),
    )
    .await
    .map_err(|_| {
        AppError::new(
            "developer_timeout",
            "Apple не ответила на запрос сессии разработчика.",
        )
    })??;
    let signer = SideloaderBuilder::<MaxCertsCallbackBox>::new(session, email)
        .machine_name("LocalSign".into())
        .storage(storage::protected_storage()?)
        .max_certs_behavior(MaxCertsBehavior::Error)
        .build();
    *state.signer.lock().await = Some(signer);
    Ok(())
}

#[tauri::command]
fn submit_two_factor(state: State<'_, Runtime>, code: String) -> Result<(), AppError> {
    if !code.is_empty() && (code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit())) {
        return Err(AppError::new(
            "two_factor_code",
            "Введите шестизначный код.",
        ));
    }
    let sender = state
        .challenge
        .lock()
        .map_err(|_| AppError::busy())?
        .take()
        .ok_or(AppError::new(
            "no_challenge",
            "Запрос подтверждения уже закрыт.",
        ))?;
    sender
        .send(code)
        .map_err(|_| AppError::new("expired_challenge", "Время подтверждения истекло."))
}

#[tauri::command]
async fn logout(state: State<'_, Runtime>) -> Result<(), AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    *state.signer.lock().await = None;
    Ok(())
}

#[tauri::command]
async fn install_ipa(app: tauri::AppHandle, state: State<'_, Runtime>) -> Result<(), AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let udid = state
        .selected_device
        .lock()
        .map_err(|_| AppError::busy())?
        .clone()
        .ok_or(AppError::new(
            "device_selection",
            "Сначала выберите iPhone.",
        ))?;
    let staged = state
        .selected_ipa
        .lock()
        .map_err(|_| AppError::busy())?
        .clone()
        .ok_or(AppError::new("ipa_selection", "Сначала выберите IPA."))?;
    let journal = data_directory(&app)?.join("install-effects");
    operation::InstallEffect::ensure_available(&journal, &udid)?;
    let check = staged.clone();
    tauri::async_runtime::spawn_blocking(move || check.verify())
        .await
        .map_err(|_| AppError::new("invalid_ipa", "Не удалось проверить копию IPA."))??;
    let provider = device::provider(&udid).await?;
    let mut guard = state.signer.lock().await;
    let signer = guard.as_mut().ok_or(AppError::new(
        "not_logged_in",
        "Сначала выполните вход Apple Account.",
    ))?;
    let info = staged.info();
    let effect = operation::InstallEffect::begin(&journal, &info.sha256, &udid, &info.bundle_id)?;
    // Persisted before polling the opaque signing/install future. Dropping that future does
    // not establish cancellation on Apple/device servers, so timeout/error keeps a durable gate.
    let result = effect
        .run(operation::INSTALL_DEADLINE, async {
            signer
                .install_app(
                    &provider,
                    info.path.clone(),
                    false,
                    None::<fn(f32) -> std::future::Ready<()>>,
                    None,
                )
                .await
                .map_err(AppError::from)
        })
        .await;
    if result.is_err() {
        // A canceled/failed opaque operation may leave signer internals partially updated.
        *guard = None;
    }
    drop(staged); // Keep the protected snapshot handle alive through the whole library operation.
    result.map(|_| ())
}

fn require_selected_device(state: &Runtime, udid: &str) -> Result<(), AppError> {
    if state
        .selected_device
        .lock()
        .map_err(|_| AppError::busy())?
        .as_deref()
        != Some(udid)
    {
        return Err(AppError::new(
            "device_selection",
            "Сначала выберите iPhone этой операции.",
        ));
    }
    Ok(())
}

#[tauri::command]
fn pending_install(
    app: tauri::AppHandle,
    state: State<'_, Runtime>,
    udid: String,
) -> Result<Option<operation::PendingInstall>, AppError> {
    require_selected_device(&state, &udid)?;
    operation::pending(&data_directory(&app)?.join("install-effects"), &udid)
}

#[tauri::command]
fn resolve_install_effect(
    app: tauri::AppHandle,
    state: State<'_, Runtime>,
    udid: String,
    effect_id: String,
) -> Result<(), AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    require_selected_device(&state, &udid)?;
    operation::resolve_after_manual_check(
        &data_directory(&app)?.join("install-effects"),
        &udid,
        &effect_id,
    )
}

fn main() {
    rustls::crypto::aws_lc_rs::default_provider()
        .install_default()
        .expect("TLS initialization failed");
    isideload::init().expect("Error initialization failed");
    // Panic payloads and dependency reports are not exported to UI or files.
    std::panic::set_hook(Box::new(|_| {}));
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).is_some_and(|s| s == "--check-frontend") {
        let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        let index = context.assets().get(&"index.html".into());
        let embedded = cfg!(feature = "custom-protocol") && index.is_some();
        let private_webview = context
            .config()
            .app
            .windows
            .first()
            .is_some_and(|window| window.incognito);
        println!(
            "{}",
            serde_json::json!({"embeddedFrontend": embedded, "assetCount": context.assets().iter().count(), "privateWebview": private_webview})
        );
        if !embedded {
            std::process::exit(1);
        }
        return;
    }
    if args.get(1).is_some_and(|s| s == "--check-ipa") {
        let result = args
            .get(2)
            .ok_or(AppError::new("ipa_path", "Укажите путь к IPA."))
            .and_then(|path| ipa::inspect(path.into()));
        match result {
            Ok(info) => println!("{}", serde_json::to_string(&info).unwrap()),
            Err(error) => {
                println!("{}", serde_json::to_string(&error).unwrap());
                std::process::exit(1);
            }
        }
        return;
    }
    if args.get(1).is_some_and(|s| s == "--diagnostics") {
        let runtime = tokio::runtime::Runtime::new().expect("Runtime unavailable");
        let result = runtime.block_on(device::list());
        let (devices, error) = match result {
            Ok(devices) => (devices.iter().map(|d| serde_json::json!({ "ios": d.version, "connection": d.connection_type })).collect::<Vec<_>>(), None),
            Err(error) => (Vec::new(), Some(error)),
        };
        println!(
            "{}",
            serde_json::json!({"product":"LocalSign", "version":env!("CARGO_PKG_VERSION"),
            "protectedStorage":storage::protected_storage().is_ok(), "devices":devices, "deviceError":error})
        );
        return;
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Runtime::default())
        .invoke_handler(tauri::generate_handler![
            protected_storage_available,
            list_devices,
            select_device,
            choose_ipa,
            login_new,
            submit_two_factor,
            logout,
            install_ipa,
            pending_install,
            resolve_install_effect
        ])
        .run(tauri::generate_context!())
        .expect("Application initialization failed");
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn requires_explicit_secure_anisette() {
        assert_eq!(
            anisette_url("https://example.com/").unwrap(),
            "https://example.com"
        );
        for url in [
            "",
            "example.com",
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com/?token=fixture",
            "https://example.com/#fragment",
        ] {
            assert!(anisette_url(url).is_err());
        }
    }
    #[test]
    fn dependency_reports_are_not_exported() {
        let error = AppError::from(rootcause::report!("fixture-sensitive-content"));
        let serialized = serde_json::to_string(&error).unwrap();
        assert!(!serialized.contains("fixture-sensitive-content"));
    }
}
