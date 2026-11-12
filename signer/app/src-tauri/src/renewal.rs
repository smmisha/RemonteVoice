//! Automatic renewal of selected apps while iReSign is running.
//!
//! A tracked app is a (iPhone, bundle id) pair with the exact IPA digest approved
//! by the user. The scheduler checks the profile expiry reported by Apple and
//! re-signs/reinstalls before it lapses. Every real install still goes through the
//! conservative install-effect journal, so an unconfirmed attempt blocks retries
//! until the user verifies the iPhone.
use crate::{
    Runtime, Signer, archive, autostart, data_directory, device,
    error::AppError,
    operation::{self, INSTALL_DEADLINE, InstallEffect},
    session,
};
use isideload::dev::app_ids::AppIdsApi;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::OpenOptions,
    io::Write,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Emitter, Manager, State};

pub const MAX_APPS: usize = 32;
const MIN_RENEW_BEFORE_HOURS: u64 = 6;
const MAX_RENEW_BEFORE_HOURS: u64 = 168;
const MIN_CHECK_INTERVAL_MINUTES: u64 = 1;
const MAX_CHECK_INTERVAL_MINUTES: u64 = 1440;
const UNKNOWN_EXPIRY_COOLDOWN_HOURS: u64 = 6;
const SUCCESS_COOLDOWN_HOURS: u64 = 6;
const FAILURE_RETRY_MINUTES: u64 = 10;
const PROFILE_QUERY_TIMEOUT: Duration = Duration::from_secs(90);
const MAX_PLAN_BYTES: usize = 128 * 1024;

const MSG_INACTIVE: &str = "Автопродление выключено.";
const MSG_NO_APPS: &str = "Нет приложений с включённым продлением.";
const MSG_WAITING: &str = "Ожидание следующей проверки срока профиля.";
const MSG_CHECKING: &str = "Проверка iPhone и сроков профиля…";
const MSG_RENEWING: &str = "Продление приложения…";
const MSG_SESSION: &str = "Нужен вход Apple Account: сессия не восстановлена.";
const MSG_DEVICE: &str = "iPhone не подключён по USB. Продление ждёт устройства.";
const MSG_BLOCKED: &str =
    "Предыдущая установка не подтверждена. Проверьте iPhone и разрешите повтор.";
const MSG_PLAN: &str = "Список автопродления недоступен или повреждён. Сбросьте список.";
const MSG_SAVE: &str = "Не удалось сохранить состояние автопродления.";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Succeeded,
    Failed,
    Blocked,
}

fn default_true() -> bool {
    true
}

fn default_schema() -> u8 {
    1
}

fn default_renew_before() -> u64 {
    48
}

fn default_check_interval() -> u64 {
    15
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TrackedApp {
    pub id: String,
    pub bundle_id: String,
    pub name: String,
    pub version: String,
    pub device_udid: String,
    pub ipa_sha256: String,
    #[serde(default)]
    pub profile_expires_at: Option<u64>,
    #[serde(default)]
    pub last_renewed_at: Option<u64>,
    #[serde(default)]
    pub last_checked_at: Option<u64>,
    #[serde(default)]
    pub last_attempt_at: Option<u64>,
    #[serde(default)]
    pub last_outcome: Option<Outcome>,
    #[serde(default)]
    pub last_error_code: Option<String>,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Plan {
    #[serde(default = "default_schema")]
    pub schema: u8,
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_renew_before")]
    pub renew_before_hours: u64,
    #[serde(default = "default_check_interval")]
    pub check_interval_minutes: u64,
    #[serde(default)]
    pub apps: Vec<TrackedApp>,
}

impl Default for Plan {
    fn default() -> Self {
        Self {
            schema: 1,
            enabled: false,
            renew_before_hours: default_renew_before(),
            check_interval_minutes: default_check_interval(),
            apps: Vec::new(),
        }
    }
}

#[derive(Clone)]
pub struct Live {
    pub scheduler: &'static str,
    pub message: &'static str,
    pub current_app: Option<String>,
    pub last_check_at: Option<u64>,
    pub next_check_at: Option<u64>,
}

impl Default for Live {
    fn default() -> Self {
        Self {
            scheduler: "inactive",
            message: MSG_INACTIVE,
            current_app: None,
            last_check_at: None,
            next_check_at: None,
        }
    }
}

#[derive(Default)]
pub struct PlanState {
    pub plan: Plan,
    pub live: Live,
    pub load_error: Option<&'static str>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RenewalStatus {
    pub enabled: bool,
    pub renew_before_hours: u64,
    pub check_interval_minutes: u64,
    pub scheduler: &'static str,
    pub scheduler_message: &'static str,
    pub current_app: Option<String>,
    pub last_check_at: Option<u64>,
    pub next_check_at: Option<u64>,
    pub autostart: bool,
    pub apps: Vec<TrackedApp>,
}

fn plan_error() -> AppError {
    AppError::new(
        "renewal_plan",
        "Список автопродления повреждён или недоступен. Сбросьте список и добавьте приложение заново.",
    )
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis() as u64)
        .unwrap_or(0)
}

pub fn plan_path(directory: &Path) -> PathBuf {
    directory.join("renewal.json")
}

pub fn tracked_id(device_udid: &str, bundle_id: &str) -> String {
    format!(
        "{:x}",
        Sha256::digest(format!("{device_udid}\n{bundle_id}").as_bytes())
    )
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn valid_bundle_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
}

fn valid_device_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
}

fn valid_text(value: &str, max: usize) -> bool {
    value.len() <= max && !value.chars().any(char::is_control)
}

fn valid_error_code(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

fn validate_plan(plan: &Plan) -> Result<(), AppError> {
    if plan.schema != 1
        || !(MIN_RENEW_BEFORE_HOURS..=MAX_RENEW_BEFORE_HOURS).contains(&plan.renew_before_hours)
        || !(MIN_CHECK_INTERVAL_MINUTES..=MAX_CHECK_INTERVAL_MINUTES)
            .contains(&plan.check_interval_minutes)
        || plan.apps.len() > MAX_APPS
    {
        return Err(plan_error());
    }
    let now = now_ms();
    let latest = now.saturating_add(10 * 365 * 24 * 60 * 60 * 1000);
    let mut ids = HashSet::new();
    for app in &plan.apps {
        if !valid_sha256(&app.ipa_sha256)
            || !valid_bundle_id(&app.bundle_id)
            || !valid_device_id(&app.device_udid)
            || !valid_text(&app.name, 256)
            || !valid_text(&app.version, 64)
            || app.id != tracked_id(&app.device_udid, &app.bundle_id)
            || !ids.insert(app.id.as_str())
            || app
                .last_error_code
                .as_deref()
                .is_some_and(|code| !valid_error_code(code))
        {
            return Err(plan_error());
        }
        for stamp in [
            app.profile_expires_at,
            app.last_renewed_at,
            app.last_checked_at,
            app.last_attempt_at,
        ]
        .into_iter()
        .flatten()
        {
            if stamp == 0 || stamp > latest {
                return Err(plan_error());
            }
        }
    }
    Ok(())
}

pub fn load_plan(directory: &Path) -> Result<Plan, AppError> {
    let path = plan_path(directory);
    let bytes = match std::fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Plan::default()),
        Err(_) => return Err(plan_error()),
    };
    if bytes.is_empty() || bytes.len() > MAX_PLAN_BYTES {
        return Err(plan_error());
    }
    let plan: Plan = serde_json::from_slice(&bytes).map_err(|_| plan_error())?;
    validate_plan(&plan)?;
    Ok(plan)
}

pub fn save_plan(directory: &Path, plan: &Plan) -> Result<(), AppError> {
    validate_plan(plan)?;
    std::fs::create_dir_all(directory).map_err(|_| plan_error())?;
    let metadata = std::fs::symlink_metadata(directory).map_err(|_| plan_error())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(plan_error());
    }
    let bytes = serde_json::to_vec_pretty(plan).map_err(|_| plan_error())?;
    if bytes.is_empty() || bytes.len() > MAX_PLAN_BYTES {
        return Err(plan_error());
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let temporary = directory.join(format!(".renewal-{}-{stamp}.tmp", std::process::id()));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| plan_error())?;
        file.write_all(&bytes).map_err(|_| plan_error())?;
        file.sync_all().map_err(|_| plan_error())?;
        drop(file);
        std::fs::rename(&temporary, plan_path(directory)).map_err(|_| plan_error())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

pub fn due(app: &TrackedApp, now: u64, renew_before_hours: u64) -> bool {
    if !app.enabled {
        return false;
    }
    if let Some(attempt) = app.last_attempt_at
        && now < attempt.saturating_add(FAILURE_RETRY_MINUTES * 60_000)
    {
        return false;
    }
    // A successful renewal installs a fresh profile. Never reinstall on every tick
    // just because the configured renewal window is as long as the profile itself
    // or because the expiry query failed after the install.
    if app.last_outcome == Some(Outcome::Succeeded)
        && let Some(renewed) = app.last_renewed_at
        && now < renewed.saturating_add(SUCCESS_COOLDOWN_HOURS * 3_600_000)
    {
        return false;
    }
    match app.profile_expires_at {
        Some(expiry) => now.saturating_add(renew_before_hours.saturating_mul(3_600_000)) >= expiry,
        None => match app.last_renewed_at {
            Some(last) => now >= last.saturating_add(UNKNOWN_EXPIRY_COOLDOWN_HOURS * 3_600_000),
            None => true,
        },
    }
}

pub fn status(state: &Runtime) -> RenewalStatus {
    let value = state
        .renewal
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let error = value.load_error.is_some();
    RenewalStatus {
        enabled: value.plan.enabled,
        renew_before_hours: value.plan.renew_before_hours,
        check_interval_minutes: value.plan.check_interval_minutes,
        scheduler: if error { "error" } else { value.live.scheduler },
        scheduler_message: if error { MSG_PLAN } else { value.live.message },
        current_app: if error {
            None
        } else {
            value.live.current_app.clone()
        },
        last_check_at: value.live.last_check_at,
        next_check_at: value.live.next_check_at,
        autostart: autostart::enabled().unwrap_or(false),
        apps: value.plan.apps.clone(),
    }
}

pub fn emit(app: &AppHandle, state: &Runtime) {
    let _ = app.emit("renewal-status", status(state));
}

fn set_live(
    state: &Runtime,
    scheduler: &'static str,
    message: &'static str,
    current_app: Option<String>,
    last_check_at: Option<u64>,
    next_check_at: Option<u64>,
) {
    let mut value = state
        .renewal
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    value.live.scheduler = scheduler;
    value.live.message = message;
    value.live.current_app = current_app;
    if last_check_at.is_some() {
        value.live.last_check_at = last_check_at;
    }
    value.live.next_check_at = next_check_at;
}

fn update_app(state: &Runtime, id: &str, change: impl FnOnce(&mut TrackedApp)) -> bool {
    let mut value = state
        .renewal
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    match value.plan.apps.iter_mut().find(|app| app.id == id) {
        Some(app) => {
            change(app);
            true
        }
        None => false,
    }
}

fn persist(app: &AppHandle, state: &Runtime) -> Result<(), AppError> {
    let directory = data_directory(app)?;
    // Keep the plan lock across the write so a command cannot replace the file with
    // a snapshot that lacks a just-recorded renewal result.
    let value = state
        .renewal
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    save_plan(&directory, &value.plan)
}

fn mark_failure(state: &Runtime, id: &str, code: &'static str, now: u64) -> bool {
    update_app(state, id, |app| {
        app.last_outcome = Some(Outcome::Failed);
        app.last_error_code = Some(code.to_string());
        app.last_attempt_at = Some(now);
        app.last_checked_at = Some(now);
    })
}

fn mark_blocked(state: &Runtime, id: &str, now: u64) -> bool {
    update_app(state, id, |app| {
        app.last_outcome = Some(Outcome::Blocked);
        app.last_error_code = Some("install_reconciliation_required".to_string());
        app.last_attempt_at = Some(now);
        app.last_checked_at = Some(now);
    })
}

pub async fn profile_expiry(signer: &mut Signer, bundle_id: &str) -> Result<Option<u64>, AppError> {
    let team = signer.get_team().await.map_err(AppError::from)?;
    let expected = format!("{bundle_id}.{}", team.team_id);
    let listed = signer
        .get_dev_session()
        .list_app_ids(&team, None)
        .await
        .map_err(AppError::from)?;
    let Some(app_id) = listed
        .app_ids
        .into_iter()
        .find(|app| app.identifier == expected)
    else {
        return Ok(None);
    };
    let profile = signer
        .get_dev_session()
        .download_team_provisioning_profile(&team, &app_id, None)
        .await
        .map_err(AppError::from)?;
    let expiry: SystemTime = profile.date_expire.into();
    let millis = expiry
        .duration_since(UNIX_EPOCH)
        .map_err(|_| {
            AppError::new(
                "profile_query",
                "Не удалось получить срок профиля из Apple. Проверьте сессию и сеть.",
            )
        })?
        .as_millis();
    Ok(Some(millis as u64))
}

/// Called after a successful manual install. If the exact app is already tracked,
/// record the fresh profile expiry and the successful renewal result.
pub async fn note_install_success(
    app: &AppHandle,
    signer: &mut Signer,
    device_udid: &str,
    bundle_id: &str,
    sha256: &str,
) {
    let state = app.state::<Runtime>();
    let id = tracked_id(device_udid, bundle_id);
    let tracked = {
        let value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        value
            .plan
            .apps
            .iter()
            .any(|tracked| tracked.id == id && tracked.ipa_sha256 == sha256)
    };
    if !tracked {
        return;
    }
    let expiry = tokio::time::timeout(PROFILE_QUERY_TIMEOUT, profile_expiry(signer, bundle_id))
        .await
        .ok()
        .and_then(Result::ok)
        .flatten();
    let now = now_ms();
    let changed = update_app(&state, &id, |tracked| {
        tracked.last_renewed_at = Some(now);
        tracked.last_attempt_at = Some(now);
        tracked.last_checked_at = Some(now);
        tracked.profile_expires_at = expiry;
        tracked.last_outcome = Some(Outcome::Succeeded);
        tracked.last_error_code = None;
    });
    if changed {
        let _ = persist(app, &state);
        emit(app, &state);
    }
}

pub async fn initialize(app: AppHandle) {
    let state = app.state::<Runtime>();
    let directory = data_directory(&app).ok();
    let loaded = match directory.clone() {
        Some(directory) => tauri::async_runtime::spawn_blocking(move || load_plan(&directory))
            .await
            .ok()
            .and_then(Result::ok),
        None => None,
    };
    {
        let mut value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match loaded {
            Some(plan) => {
                value.plan = plan;
                value.load_error = None;
            }
            None => value.load_error = Some(MSG_PLAN),
        }
        value.live = Live::default();
        value.live.next_check_at = Some(now_ms().saturating_add(30_000));
    }
    emit(&app, &state);
    restore_session(&app).await;
    scheduler(app).await;
}

async fn restore_session(app: &AppHandle) {
    let state = app.state::<Runtime>();
    let _busy = state.busy.lock().await;
    if state.signer.lock().await.is_some() {
        return;
    }
    let stored = session::stored_async().await.unwrap_or(false);
    let loaded = session::load_record_async().await;
    match loaded {
        Ok(Some(record)) => {
            let email = record.email.clone();
            {
                let mut view = state
                    .session
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                *view = session::SessionView {
                    status: "restoring",
                    email: Some(email.clone()),
                    stored: true,
                };
            }
            session::emit(app, &state);
            match session::restore_signer(record).await {
                Ok(signer) => {
                    *state.signer.lock().await = Some(signer);
                    let mut view = state
                        .session
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    *view = session::SessionView {
                        status: "active",
                        email: Some(email),
                        stored: true,
                    };
                }
                Err(_) => {
                    let mut view = state
                        .session
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    *view = session::SessionView {
                        status: "failed",
                        email: Some(email),
                        stored: true,
                    };
                }
            }
        }
        Ok(None) => {
            let mut view = state
                .session
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *view = session::SessionView::default();
        }
        Err(_) => {
            let mut view = state
                .session
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *view = session::SessionView {
                status: "failed",
                email: None,
                stored,
            };
        }
    }
    session::emit(app, &state);
}

pub async fn scheduler(app: AppHandle) {
    loop {
        let state = app.state::<Runtime>();
        let next = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .live
            .next_check_at;
        let wait = next
            .map(|value| value.saturating_sub(now_ms()))
            .unwrap_or(1_000)
            .clamp(1_000, 300_000);
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_millis(wait)) => {}
            _ = state.renewal_kick.notified() => {}
        }
        tick(&app).await;
    }
}

async fn tick(app: &AppHandle) {
    let state = app.state::<Runtime>();
    let _busy = match state.busy.try_lock() {
        Ok(guard) => guard,
        Err(_) => {
            // Another operation owns the device/signer. Recheck later instead of
            // spinning on an already elapsed deadline.
            let mut value = state
                .renewal
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            value.live.next_check_at = Some(now_ms().saturating_add(30_000));
            return;
        }
    };
    let now = now_ms();
    let (plan, load_error) = {
        let value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        (value.plan.clone(), value.load_error.is_some())
    };
    let interval = plan.check_interval_minutes.saturating_mul(60_000);
    let next = now.saturating_add(interval);
    if load_error {
        set_live(&state, "error", MSG_PLAN, None, Some(now), Some(next));
        emit(app, &state);
        return;
    }
    if !plan.enabled {
        set_live(
            &state,
            "inactive",
            MSG_INACTIVE,
            None,
            Some(now),
            Some(next),
        );
        emit(app, &state);
        return;
    }
    if plan.apps.iter().all(|app| !app.enabled) {
        set_live(&state, "inactive", MSG_NO_APPS, None, Some(now), Some(next));
        emit(app, &state);
        return;
    }
    let due_apps: Vec<TrackedApp> = plan
        .apps
        .iter()
        .filter(|app| due(app, now, plan.renew_before_hours))
        .cloned()
        .collect();
    if due_apps.is_empty() {
        set_live(&state, "waiting", MSG_WAITING, None, Some(now), Some(next));
        emit(app, &state);
        return;
    }
    if state.signer.lock().await.is_none() {
        set_live(
            &state,
            "session_required",
            MSG_SESSION,
            None,
            Some(now),
            Some(next),
        );
        emit(app, &state);
        return;
    }
    set_live(&state, "checking", MSG_CHECKING, None, None, None);
    emit(app, &state);
    let devices = match device::list().await {
        Ok(devices) => devices,
        Err(_) => {
            set_live(
                &state,
                "device_unavailable",
                MSG_DEVICE,
                None,
                Some(now),
                Some(now.saturating_add(120_000)),
            );
            emit(app, &state);
            return;
        }
    };
    let Ok(directory) = data_directory(app) else {
        set_live(&state, "error", MSG_SAVE, None, Some(now), Some(next));
        emit(app, &state);
        return;
    };
    let archive_directory = archive::directory(&directory);
    let journal = directory.join("install-effects");
    let mut changed = false;
    let mut stopped = false;
    let mut final_kind = ("waiting", MSG_WAITING, None);
    for tracked in due_apps {
        if stopped {
            break;
        }
        if !devices
            .iter()
            .any(|device| device.udid == tracked.device_udid)
        {
            final_kind = ("device_unavailable", MSG_DEVICE, Some(tracked.name.clone()));
            continue;
        }
        if InstallEffect::ensure_available(&journal, &tracked.device_udid).is_err() {
            changed |= mark_blocked(&state, &tracked.id, now);
            final_kind = ("blocked", MSG_BLOCKED, Some(tracked.name.clone()));
            continue;
        }
        let path = match archive::verify(&archive_directory, &tracked.ipa_sha256) {
            Ok(path) => path,
            Err(_) => {
                changed |= mark_failure(&state, &tracked.id, "renewal_ipa", now);
                continue;
            }
        };
        set_live(
            &state,
            "renewing",
            MSG_RENEWING,
            Some(tracked.name.clone()),
            None,
            None,
        );
        emit(app, &state);
        let provider = match device::provider(&tracked.device_udid).await {
            Ok(provider) => provider,
            Err(_) => {
                changed |= mark_failure(&state, &tracked.id, "device", now);
                continue;
            }
        };
        let mut guard = state.signer.lock().await;
        let Some(signer) = guard.as_mut() else {
            break;
        };
        let effect = match InstallEffect::begin(
            &journal,
            &tracked.ipa_sha256,
            &tracked.device_udid,
            &tracked.bundle_id,
        ) {
            Ok(effect) => effect,
            Err(_) => {
                changed |= mark_blocked(&state, &tracked.id, now);
                final_kind = ("blocked", MSG_BLOCKED, Some(tracked.name.clone()));
                break;
            }
        };
        let result = effect
            .run(INSTALL_DEADLINE, async {
                signer
                    .install_app(
                        &provider,
                        path,
                        false,
                        None::<fn(f32) -> std::future::Ready<()>>,
                        None,
                    )
                    .await
                    .map_err(AppError::from)
            })
            .await;
        match result {
            Ok(_) => {
                let expiry = tokio::time::timeout(
                    PROFILE_QUERY_TIMEOUT,
                    profile_expiry(signer, &tracked.bundle_id),
                )
                .await
                .ok()
                .and_then(Result::ok)
                .flatten();
                changed |= update_app(&state, &tracked.id, |app| {
                    app.last_renewed_at = Some(now);
                    app.last_attempt_at = Some(now);
                    app.last_checked_at = Some(now);
                    app.profile_expires_at = expiry;
                    app.last_outcome = Some(Outcome::Succeeded);
                    app.last_error_code = None;
                });
            }
            Err(error) => {
                let code = error.code;
                changed |= mark_failure(&state, &tracked.id, code, now);
                final_kind = ("blocked", MSG_BLOCKED, Some(tracked.name.clone()));
                stopped = true;
                *guard = None;
                session::mark_inactive(app, &state);
            }
        }
    }
    if changed && persist(app, &state).is_err() {
        set_live(&state, "error", MSG_SAVE, None, Some(now), Some(next));
        emit(app, &state);
        return;
    }
    set_live(
        &state,
        final_kind.0,
        final_kind.1,
        final_kind.2,
        Some(now),
        Some(next),
    );
    emit(app, &state);
}

fn require_tracked(state: &Runtime, id: &str) -> Result<TrackedApp, AppError> {
    state
        .renewal
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .plan
        .apps
        .iter()
        .find(|app| app.id == id)
        .cloned()
        .ok_or_else(|| {
            AppError::new(
                "renewal_app",
                "Приложение не найдено в списке автопродления. Обновите состояние.",
            )
        })
}

#[tauri::command]
pub fn renewal_status(state: State<'_, Runtime>) -> RenewalStatus {
    status(&state)
}

#[tauri::command]
pub fn set_renewal_settings(
    app: AppHandle,
    state: State<'_, Runtime>,
    enabled: bool,
    renew_before_hours: u64,
    check_interval_minutes: u64,
) -> Result<RenewalStatus, AppError> {
    if !(MIN_RENEW_BEFORE_HOURS..=MAX_RENEW_BEFORE_HOURS).contains(&renew_before_hours)
        || !(MIN_CHECK_INTERVAL_MINUTES..=MAX_CHECK_INTERVAL_MINUTES)
            .contains(&check_interval_minutes)
    {
        return Err(plan_error());
    }
    let directory = data_directory(&app)?;
    {
        let mut value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if value.load_error.is_some() {
            return Err(plan_error());
        }
        value.plan.enabled = enabled;
        value.plan.renew_before_hours = renew_before_hours;
        value.plan.check_interval_minutes = check_interval_minutes;
        save_plan(&directory, &value.plan)?;
    }
    set_live(
        &state,
        if enabled { "waiting" } else { "inactive" },
        if enabled { MSG_WAITING } else { MSG_INACTIVE },
        None,
        None,
        Some(now_ms()),
    );
    state.renewal_kick.notify_one();
    emit(&app, &state);
    Ok(status(&state))
}

#[tauri::command]
pub fn set_tracked_app_enabled(
    app: AppHandle,
    state: State<'_, Runtime>,
    id: String,
    enabled: bool,
) -> Result<RenewalStatus, AppError> {
    let directory = data_directory(&app)?;
    {
        let mut value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if value.load_error.is_some() {
            return Err(plan_error());
        }
        let app = value
            .plan
            .apps
            .iter_mut()
            .find(|app| app.id == id)
            .ok_or_else(|| {
                AppError::new(
                    "renewal_app",
                    "Приложение не найдено в списке автопродления. Обновите состояние.",
                )
            })?;
        app.enabled = enabled;
        save_plan(&directory, &value.plan)?;
    }
    if enabled {
        set_live(&state, "waiting", MSG_WAITING, None, None, Some(now_ms()));
        state.renewal_kick.notify_one();
    }
    emit(&app, &state);
    Ok(status(&state))
}

#[tauri::command]
pub async fn track_current_app(
    app: AppHandle,
    state: State<'_, Runtime>,
) -> Result<RenewalStatus, AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let udid = state
        .selected_device
        .lock()
        .map_err(|_| AppError::busy())?
        .clone()
        .ok_or_else(|| AppError::new("device_selection", "Сначала выберите iPhone."))?;
    let staged = state
        .selected_ipa
        .lock()
        .map_err(|_| AppError::busy())?
        .clone()
        .ok_or_else(|| AppError::new("ipa_selection", "Сначала выберите IPA."))?;
    if state.signer.lock().await.is_none() {
        return Err(AppError::new(
            "not_logged_in",
            "Сначала выполните вход Apple Account.",
        ));
    }
    let info = staged.info().clone();
    let check = staged.clone();
    tauri::async_runtime::spawn_blocking(move || check.verify())
        .await
        .map_err(|_| AppError::new("invalid_ipa", "Не удалось проверить копию IPA."))??;
    let directory = data_directory(&app)?;
    let archive_directory = archive::directory(&directory);
    let source = info.path.clone();
    let sha256 = info.sha256.clone();
    tauri::async_runtime::spawn_blocking(move || {
        archive::ensure(&archive_directory, &source, &sha256)
    })
    .await
    .map_err(|_| AppError::new("renewal_ipa", "Не удалось сохранить копию IPA."))??;
    let expiry = {
        let mut guard = state.signer.lock().await;
        let signer = guard.as_mut().ok_or_else(|| {
            AppError::new("not_logged_in", "Сначала выполните вход Apple Account.")
        })?;
        tokio::time::timeout(
            PROFILE_QUERY_TIMEOUT,
            profile_expiry(signer, &info.bundle_id),
        )
        .await
        .ok()
        .and_then(Result::ok)
        .flatten()
    };
    let now = now_ms();
    let keep: HashSet<String>;
    {
        let mut value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if value.load_error.is_some() {
            return Err(plan_error());
        }
        let id = tracked_id(&udid, &info.bundle_id);
        if let Some(existing) = value.plan.apps.iter_mut().find(|app| app.id == id) {
            existing.name = info.name.clone();
            existing.version = info.version.clone();
            existing.ipa_sha256 = info.sha256.clone();
            existing.enabled = true;
            if let Some(expiry) = expiry {
                existing.profile_expires_at = Some(expiry);
            }
        } else {
            if value.plan.apps.len() >= MAX_APPS {
                return Err(AppError::new(
                    "renewal_limit",
                    "Достигнут предел отслеживаемых приложений.",
                ));
            }
            value.plan.apps.push(TrackedApp {
                id,
                bundle_id: info.bundle_id.clone(),
                name: info.name.clone(),
                version: info.version.clone(),
                device_udid: udid.clone(),
                ipa_sha256: info.sha256.clone(),
                profile_expires_at: expiry,
                last_renewed_at: None,
                last_checked_at: None,
                last_attempt_at: None,
                last_outcome: None,
                last_error_code: None,
                enabled: true,
            });
        }
        value.plan.enabled = true;
        save_plan(&directory, &value.plan)?;
        keep = value
            .plan
            .apps
            .iter()
            .map(|app| app.ipa_sha256.clone())
            .collect();
    }
    let cleanup_directory = archive::directory(&directory);
    tauri::async_runtime::spawn_blocking(move || archive::cleanup(&cleanup_directory, &keep))
        .await
        .ok();
    set_live(&state, "waiting", MSG_WAITING, None, None, Some(now));
    state.renewal_kick.notify_one();
    emit(&app, &state);
    Ok(status(&state))
}

#[tauri::command]
pub async fn untrack_app(
    app: AppHandle,
    state: State<'_, Runtime>,
    id: String,
) -> Result<RenewalStatus, AppError> {
    // The busy guard prevents deleting an archived IPA while a renewal is reading it.
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let directory = data_directory(&app)?;
    let keep: HashSet<String>;
    {
        let mut value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if value.load_error.is_some() {
            return Err(plan_error());
        }
        value.plan.apps.retain(|app| app.id != id);
        save_plan(&directory, &value.plan)?;
        keep = value
            .plan
            .apps
            .iter()
            .map(|app| app.ipa_sha256.clone())
            .collect();
    }
    archive::cleanup(&archive::directory(&directory), &keep);
    emit(&app, &state);
    Ok(status(&state))
}

#[tauri::command]
pub async fn renew_now(
    app: AppHandle,
    state: State<'_, Runtime>,
    id: String,
) -> Result<RenewalStatus, AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let tracked = require_tracked(&state, &id)?;
    let directory = data_directory(&app)?;
    let journal = directory.join("install-effects");
    if InstallEffect::ensure_available(&journal, &tracked.device_udid).is_err() {
        mark_blocked(&state, &tracked.id, now_ms());
        let _ = persist(&app, &state);
        emit(&app, &state);
        return Err(operation::reconciliation_required());
    }
    let path = archive::verify(&archive::directory(&directory), &tracked.ipa_sha256)?;
    let devices = device::list().await?;
    if !devices
        .iter()
        .any(|device| device.udid == tracked.device_udid)
    {
        return Err(AppError::new(
            "device",
            "Выбранный iPhone не подключён. Подключите его по USB и разблокируйте.",
        ));
    }
    let provider = device::provider(&tracked.device_udid).await?;
    let now = now_ms();
    let mut guard = state.signer.lock().await;
    let signer = guard
        .as_mut()
        .ok_or_else(|| AppError::new("not_logged_in", "Сначала выполните вход Apple Account."))?;
    let effect = InstallEffect::begin(
        &journal,
        &tracked.ipa_sha256,
        &tracked.device_udid,
        &tracked.bundle_id,
    )?;
    set_live(
        &state,
        "renewing",
        MSG_RENEWING,
        Some(tracked.name.clone()),
        None,
        None,
    );
    emit(&app, &state);
    let result = effect
        .run(INSTALL_DEADLINE, async {
            signer
                .install_app(
                    &provider,
                    path,
                    false,
                    None::<fn(f32) -> std::future::Ready<()>>,
                    None,
                )
                .await
                .map_err(AppError::from)
        })
        .await;
    match result {
        Ok(_) => {
            let expiry = tokio::time::timeout(
                PROFILE_QUERY_TIMEOUT,
                profile_expiry(signer, &tracked.bundle_id),
            )
            .await
            .ok()
            .and_then(Result::ok)
            .flatten();
            update_app(&state, &tracked.id, |app| {
                app.last_renewed_at = Some(now);
                app.last_attempt_at = Some(now);
                app.last_checked_at = Some(now);
                app.profile_expires_at = expiry;
                app.last_outcome = Some(Outcome::Succeeded);
                app.last_error_code = None;
            });
            let persisted = persist(&app, &state);
            set_live(
                &state,
                "waiting",
                MSG_WAITING,
                None,
                Some(now),
                Some(now_ms()),
            );
            emit(&app, &state);
            persisted?;
            Ok(status(&state))
        }
        Err(error) => {
            let failed_at = now_ms();
            mark_failure(&state, &tracked.id, error.code, failed_at);
            let _ = persist(&app, &state);
            set_live(
                &state,
                "blocked",
                MSG_BLOCKED,
                None,
                Some(failed_at),
                Some(now_ms()),
            );
            *guard = None;
            session::mark_inactive(&app, &state);
            emit(&app, &state);
            Err(error)
        }
    }
}

#[tauri::command]
pub fn check_renewals_now(app: AppHandle, state: State<'_, Runtime>) -> RenewalStatus {
    set_live(&state, "waiting", MSG_WAITING, None, None, Some(now_ms()));
    state.renewal_kick.notify_one();
    emit(&app, &state);
    status(&state)
}

#[tauri::command]
pub fn reset_renewal_plan(
    app: AppHandle,
    state: State<'_, Runtime>,
) -> Result<RenewalStatus, AppError> {
    let _busy = state.busy.try_lock().map_err(|_| AppError::busy())?;
    let directory = data_directory(&app)?;
    let path = plan_path(&directory);
    if path.exists() {
        let archive = directory.join(format!("renewal.corrupt-{}.json", now_ms()));
        std::fs::rename(&path, &archive).map_err(|_| plan_error())?;
    }
    {
        let mut value = state
            .renewal
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        value.plan = Plan::default();
        value.load_error = None;
        value.live = Live::default();
        value.live.next_check_at = Some(now_ms());
        save_plan(&directory, &value.plan)?;
    }
    archive::cleanup(&archive::directory(&directory), &HashSet::new());
    state.renewal_kick.notify_one();
    emit(&app, &state);
    Ok(status(&state))
}

#[tauri::command]
pub fn set_autostart(
    app: AppHandle,
    state: State<'_, Runtime>,
    enabled: bool,
) -> Result<RenewalStatus, AppError> {
    autostart::set(enabled)?;
    emit(&app, &state);
    Ok(status(&state))
}

/// Read-only summary for `--check-renewal`. Device UDIDs are hashed.
pub fn check_summary(directory: &Path) -> serde_json::Value {
    let (plan, error) = match load_plan(directory) {
        Ok(plan) => (plan, None),
        Err(_) => (Plan::default(), Some("renewal_plan")),
    };
    let apps: Vec<serde_json::Value> = plan
        .apps
        .iter()
        .map(|app| {
            serde_json::json!({
                "id": app.id,
                "bundleId": app.bundle_id,
                "device": format!("{:x}", Sha256::digest(app.device_udid.as_bytes()))[..12].to_string(),
                "profileExpiresAt": app.profile_expires_at,
                "lastOutcome": app.last_outcome,
                "enabled": app.enabled,
            })
        })
        .collect();
    serde_json::json!({
        "schema": 1,
        "enabled": plan.enabled,
        "renewBeforeHours": plan.renew_before_hours,
        "checkIntervalMinutes": plan.check_interval_minutes,
        "apps": apps,
        "autostart": autostart::enabled().unwrap_or(false),
        "error": error,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "iresign-renewal-test-{}-{}-{}",
                std::process::id(),
                now_ms(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            assert!(self.0.starts_with(std::env::temp_dir()));
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn app() -> TrackedApp {
        let udid = "fixture-device";
        let bundle = "dev.iresign.fixture";
        TrackedApp {
            id: tracked_id(udid, bundle),
            bundle_id: bundle.into(),
            name: "Fixture".into(),
            version: "1.0".into(),
            device_udid: udid.into(),
            ipa_sha256: "a".repeat(64),
            profile_expires_at: Some(10_000_000),
            last_renewed_at: None,
            last_checked_at: None,
            last_attempt_at: None,
            last_outcome: None,
            last_error_code: None,
            enabled: true,
        }
    }

    fn plan() -> Plan {
        Plan {
            schema: 1,
            enabled: true,
            renew_before_hours: 48,
            check_interval_minutes: 15,
            apps: vec![app()],
        }
    }

    #[test]
    fn plan_roundtrip_is_atomic_and_leaves_no_temporary_file() {
        let fixture = Fixture::new();
        let value = plan();
        save_plan(&fixture.0, &value).unwrap();
        let loaded = load_plan(&fixture.0).unwrap();
        assert_eq!(loaded.apps.len(), 1);
        assert_eq!(loaded.apps[0].bundle_id, "dev.iresign.fixture");
        assert_eq!(
            loaded.apps[0].id,
            tracked_id("fixture-device", "dev.iresign.fixture")
        );
        let leftovers: Vec<_> = std::fs::read_dir(&fixture.0)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().starts_with(".renewal-"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn missing_plan_is_an_empty_disabled_plan() {
        let fixture = Fixture::new();
        let loaded = load_plan(&fixture.0).unwrap();
        assert!(!loaded.enabled);
        assert!(loaded.apps.is_empty());
        assert_eq!(loaded.schema, 1);
    }

    #[test]
    fn corrupt_plan_fails_closed_and_can_be_reported() {
        let fixture = Fixture::new();
        std::fs::write(plan_path(&fixture.0), b"{not json").unwrap();
        assert_eq!(load_plan(&fixture.0).unwrap_err().code, "renewal_plan");
        let summary = check_summary(&fixture.0);
        assert_eq!(summary["error"], "renewal_plan");
        assert_eq!(summary["apps"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn rejects_tampered_or_traversing_plan_fields() {
        let fixture = Fixture::new();
        let mut value = plan();
        value.apps[0].ipa_sha256 = "../../escape".into();
        assert_eq!(
            save_plan(&fixture.0, &value).unwrap_err().code,
            "renewal_plan"
        );

        let mut value = plan();
        value.apps[0].bundle_id = "../escape".into();
        assert_eq!(
            save_plan(&fixture.0, &value).unwrap_err().code,
            "renewal_plan"
        );

        let mut value = plan();
        value.apps[0].id = "0".repeat(64);
        assert_eq!(
            save_plan(&fixture.0, &value).unwrap_err().code,
            "renewal_plan"
        );

        let mut value = plan();
        value.renew_before_hours = 0;
        assert_eq!(
            save_plan(&fixture.0, &value).unwrap_err().code,
            "renewal_plan"
        );

        let mut value = plan();
        value.apps.push(value.apps[0].clone());
        assert_eq!(
            save_plan(&fixture.0, &value).unwrap_err().code,
            "renewal_plan"
        );
    }

    #[test]
    fn due_uses_expiry_window_unknown_cooldown_and_retry_delay() {
        let now = 1_000_000_000u64;
        let mut value = app();
        value.profile_expires_at = Some(now + 49 * 3_600_000);
        assert!(!due(&value, now, 48));
        value.profile_expires_at = Some(now + 47 * 3_600_000);
        assert!(due(&value, now, 48));

        let mut unknown = app();
        unknown.profile_expires_at = None;
        unknown.last_renewed_at = Some(now - 7 * 3_600_000);
        assert!(due(&unknown, now, 48));
        unknown.last_renewed_at = Some(now - 3_600_000);
        assert!(!due(&unknown, now, 48));

        let mut failed = app();
        failed.profile_expires_at = Some(now);
        failed.last_attempt_at = Some(now - 60_000);
        assert!(!due(&failed, now, 48));
        failed.last_attempt_at = Some(now - 11 * 60_000);
        assert!(due(&failed, now, 48));

        let mut succeeded = app();
        succeeded.profile_expires_at = Some(now + 3_600_000);
        succeeded.last_renewed_at = Some(now - 3_600_000);
        succeeded.last_outcome = Some(Outcome::Succeeded);
        assert!(!due(&succeeded, now, 168));
        succeeded.last_renewed_at = Some(now - 7 * 3_600_000);
        assert!(due(&succeeded, now, 168));

        let mut disabled = app();
        disabled.enabled = false;
        assert!(!due(&disabled, now, 48));
    }

    #[test]
    fn repeated_save_replaces_previous_plan() {
        let fixture = Fixture::new();
        let mut value = plan();
        save_plan(&fixture.0, &value).unwrap();
        value.enabled = false;
        value.apps[0].name = "Updated".into();
        save_plan(&fixture.0, &value).unwrap();
        let loaded = load_plan(&fixture.0).unwrap();
        assert!(!loaded.enabled);
        assert_eq!(loaded.apps[0].name, "Updated");
    }

    #[test]
    fn summary_does_not_expose_raw_device_udid() {
        let fixture = Fixture::new();
        save_plan(&fixture.0, &plan()).unwrap();
        let summary = serde_json::to_string(&check_summary(&fixture.0)).unwrap();
        assert!(!summary.contains("fixture-device"));
        assert!(summary.contains("bundleId"));
    }
}
