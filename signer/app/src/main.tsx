import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import "./style.css";

type Device = { name: string; udid: string; version: string; connectionType: string };
type Ipa = { name: string; bundleId: string; version: string; sha256: string };
type Failure = { code: string; message: string };
type PendingInstall = { effectId: string; artifactSha256: string; targetDevice: string; bundleId: string; phase: string };
type SessionState = { status: "absent" | "restoring" | "active" | "failed"; email: string | null; stored: boolean };
type TrackedApp = {
  id: string;
  bundleId: string;
  name: string;
  version: string;
  deviceUdid: string;
  profileExpiresAt: number | null;
  lastRenewedAt: number | null;
  lastCheckedAt: number | null;
  lastOutcome: "succeeded" | "failed" | "blocked" | null;
  lastErrorCode: string | null;
  enabled: boolean;
};
type RenewalStatus = {
  enabled: boolean;
  renewBeforeHours: number;
  checkIntervalMinutes: number;
  scheduler: "inactive" | "waiting" | "checking" | "renewing" | "session_required" | "device_unavailable" | "blocked" | "error";
  schedulerMessage: string;
  currentApp: string | null;
  lastCheckAt: number | null;
  nextCheckAt: number | null;
  autostart: boolean;
  apps: TrackedApp[];
};

const native = "__TAURI_INTERNALS__" in window;
const EMPTY_SESSION: SessionState = { status: "absent", email: null, stored: false };
const RESTORING_SESSION: SessionState = { status: "restoring", email: null, stored: false };
const EMPTY_RENEWAL: RenewalStatus = {
  enabled: false,
  renewBeforeHours: 48,
  checkIntervalMinutes: 15,
  scheduler: "inactive",
  schedulerMessage: "Автопродление выключено.",
  currentApp: null,
  lastCheckAt: null,
  nextCheckAt: null,
  autostart: false,
  apps: [],
};
const RENEWAL_ERRORS: Record<string, string> = {
  renewal_ipa: "Копия IPA отсутствует или повреждена",
  profile_query: "Не удалось получить срок профиля",
  session_required: "Нужен вход Apple Account",
  not_logged_in: "Нужен вход Apple Account",
  device: "iPhone недоступен",
  device_timeout: "iPhone недоступен",
  install_unconfirmed: "Нужна проверка установки на iPhone",
  install_timeout_unknown: "Нужна проверка установки на iPhone",
  install_reconciliation_required: "Нужна проверка установки на iPhone",
  operation_journal: "Журнал операции недоступен",
};
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });

function failureMessage(value: unknown): string {
  if (value && typeof value === "object" && "code" in value && "message" in value) {
    return String((value as Failure).message);
  }
  return "Операция не выполнена. Повторите после проверки подключения.";
}

function formatTimestamp(value: number | null): string | null {
  if (value === null) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : dateFormat.format(date);
}

function renewalErrorText(code: string | null): string {
  const text = code !== null && Object.hasOwn(RENEWAL_ERRORS, code) ? RENEWAL_ERRORS[code] : "";
  return text || "Ошибка продления";
}

function Outcome({ app }: { app: TrackedApp }) {
  if (app.lastOutcome === "succeeded") return <p className="outcome ok">Успешно</p>;
  if (app.lastOutcome === "failed") return <p className="outcome bad">{renewalErrorText(app.lastErrorCode)}</p>;
  if (app.lastOutcome === "blocked") return <p className="outcome warn">Требуется проверка предыдущей установки на iPhone</p>;
  return <p className="outcome">Ожидает проверки</p>;
}

function App() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState("");
  const [ipa, setIpa] = useState<Ipa | null>(null);
  const [protectedStorage, setProtectedStorage] = useState<boolean | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [anisette, setAnisette] = useState("");
  const [persistSession, setPersistSession] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState(false);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  const [eventsReady, setEventsReady] = useState(false);
  const [pendingInstall, setPendingInstall] = useState<PendingInstall | null>(null);
  const [session, setSession] = useState<SessionState>(() => (native ? RESTORING_SESSION : EMPTY_SESSION));
  const [renewal, setRenewal] = useState<RenewalStatus>(() => ({
    ...EMPTY_RENEWAL,
    schedulerMessage: native ? "Проверка состояния автопродления…" : "Автопродление недоступно в режиме просмотра.",
  }));
  const running = useRef(false);

  function applySession(state: SessionState) {
    setSession(state);
    if (state.status === "active") setLoggedIn(true);
    else if (state.status === "absent" || state.status === "failed") setLoggedIn(false);
  }

  useEffect(() => {
    if (!native) return;
    let disposed = false;
    const unlisteners: UnlistenFn[] = [];
    const keep = (unlisten: UnlistenFn) => { if (disposed) unlisten(); else unlisteners.push(unlisten); };
    void (async () => {
      try {
        keep(await listen("two-factor-required", () => { setCode(""); setChallenge(true); }));
        keep(await listen("two-factor-closed", () => { setCode(""); setChallenge(false); }));
        keep(await listen<SessionState>("session-state", event => { if (!disposed) applySession(event.payload); }));
        keep(await listen<RenewalStatus>("renewal-status", event => { if (!disposed) setRenewal(event.payload); }));
        if (disposed) return;
        setEventsReady(true);
        const [storage, deviceResult, sessionResult, renewalResult] = await Promise.allSettled([
          invoke<boolean>("protected_storage_available"),
          invoke<Device[]>("list_devices"),
          invoke<SessionState>("session_state"),
          invoke<RenewalStatus>("renewal_status"),
        ]);
        if (disposed) return;
        setProtectedStorage(storage.status === "fulfilled" ? storage.value : false);
        if (sessionResult.status === "fulfilled") applySession(sessionResult.value);
        else setSession(EMPTY_SESSION);
        if (renewalResult.status === "fulfilled") setRenewal(renewalResult.value);
        if (deviceResult.status === "fulfilled") {
          setDevices(deviceResult.value);
          setMessage(deviceResult.value.length ? "iPhone обнаружен. Выберите устройство." : "Подключите iPhone по USB и разблокируйте его.");
        } else {
          setError(true);
          setMessage(failureMessage(deviceResult.reason));
        }
      } catch {
        if (!disposed) {
          setSession(EMPTY_SESSION);
          setError(true);
          setMessage("Не удалось инициализировать приложение. Перезапустите iReSign.");
        }
      }
    })();
    return () => { disposed = true; unlisteners.forEach(fn => fn()); };
  }, []);

  async function run(label: string, action: () => Promise<void>) {
    if (running.current) return;
    running.current = true;
    setBusy(true); setError(false); setMessage(label);
    try { await action(); }
    catch (value) { setError(true); setMessage(failureMessage(value)); }
    finally { running.current = false; setBusy(false); }
  }

  async function refreshSessionAndRenewal() {
    const [sessionResult, renewalResult] = await Promise.allSettled([
      invoke<SessionState>("session_state"),
      invoke<RenewalStatus>("renewal_status"),
    ]);
    if (sessionResult.status === "fulfilled") applySession(sessionResult.value);
    if (renewalResult.status === "fulfilled") setRenewal(renewalResult.value);
  }

  async function refreshRenewal() {
    try { setRenewal(await invoke<RenewalStatus>("renewal_status")); }
    catch { /* The renewal panel keeps its last known state. */ }
  }

  async function refresh() {
    await run("Поиск iPhone…", async () => {
      const found = await invoke<Device[]>("list_devices");
      setDevices(found);
      if (!found.some(d => d.udid === selected)) { setSelected(""); setPendingInstall(null); }
      else if (selected) setPendingInstall(await invoke<PendingInstall | null>("pending_install", { udid: selected }));
      setMessage(found.length ? "Выберите iPhone из списка." : "Подключите iPhone по USB и разблокируйте его.");
    });
  }

  async function chooseDevice(udid: string) {
    await run("Проверка iPhone…", async () => {
      await invoke("select_device", { udid });
      setSelected(udid); setPendingInstall(null);
      setPendingInstall(await invoke<PendingInstall | null>("pending_install", { udid }));
      setMessage("iPhone выбран.");
    });
  }

  async function chooseIpa() {
    await run("Выбор и проверка IPA…", async () => {
      const info = await invoke<Ipa | null>("choose_ipa");
      if (info) { setIpa(info); setMessage("IPA проверен. Проверьте название приложения перед установкой."); }
      else setMessage("Выбор файла отменён.");
    });
  }

  async function login(event: React.FormEvent) {
    event.preventDefault();
    const value = password;
    setPassword("");
    await run("Вход Apple Account…", async () => {
      await invoke("login_new", { email, password: value, anisetteServer: anisette, persistSession });
      setLoggedIn(true);
      setSession({ status: "active", email, stored: persistSession });
      await refreshSessionAndRenewal();
      setMessage("Вход выполнен. Можно подписать и установить выбранный IPA.");
    });
  }

  async function logout() {
    await run("Завершение сессии…", async () => {
      await invoke("logout");
      setLoggedIn(false);
      setSession(EMPTY_SESSION);
      await refreshSessionAndRenewal();
      setMessage("Сессия завершена.");
    });
  }

  async function forgetSession() {
    await run("Удаление сохранённой сессии…", async () => {
      applySession(await invoke<SessionState>("forget_stored_session"));
      setLoggedIn(false);
      await refreshRenewal();
      setMessage("Сохранённая сессия удалена.");
    });
  }

  async function submitCode(event: React.FormEvent) {
    event.preventDefault();
    const value = code; setCode("");
    try { await invoke("submit_two_factor", { code: value }); setChallenge(false); }
    catch (value) { setError(true); setMessage(failureMessage(value)); }
  }

  async function install() {
    await run("Подпись и установка… Не отключайте iPhone.", async () => {
      try {
        await invoke("install_ipa");
        setPendingInstall(null);
        await refreshRenewal();
        setMessage("Установка завершена. Проверьте запуск приложения на iPhone; при необходимости включите режим разработчика и доверьте профилю.");
      } catch (value) {
        const code = value && typeof value === "object" && "code" in value ? String(value.code) : "";
        if (["install_unconfirmed", "install_timeout_unknown", "operation_journal", "not_logged_in"].includes(code)) setLoggedIn(false);
        try { setPendingInstall(await invoke<PendingInstall | null>("pending_install", { udid: selected })); }
        catch { /* Keep the original fixed error; the backend still blocks an unresolved effect. */ }
        throw value;
      }
    });
  }

  async function resolveInstall() {
    if (!pendingInstall || pendingInstall.targetDevice !== selected) return;
    const effectId = pendingInstall.effectId;
    await run("Сохранение решения после ручной проверки…", async () => {
      await invoke("resolve_install_effect", { udid: selected, effectId });
      setPendingInstall(await invoke<PendingInstall | null>("pending_install", { udid: selected }));
      setMessage("Решение сохранено. Новая установка не запускалась. Для отдельной попытки выполните вход при необходимости и нажмите «Подписать и установить».");
    });
  }

  async function saveRenewalSettings(
    patch: Partial<Pick<RenewalStatus, "enabled" | "renewBeforeHours" | "checkIntervalMinutes">>,
    label: string,
  ) {
    const next = {
      enabled: renewal.enabled,
      renewBeforeHours: renewal.renewBeforeHours,
      checkIntervalMinutes: renewal.checkIntervalMinutes,
      ...patch,
    };
    await run(label, async () => {
      setRenewal(await invoke<RenewalStatus>("set_renewal_settings", next));
      setMessage("Настройки автопродления сохранены.");
    });
  }

  async function setRenewalEnabled(enabled: boolean) {
    await saveRenewalSettings({ enabled }, "Сохранение настроек автопродления…");
  }

  async function setRenewBefore(hours: number) {
    await saveRenewalSettings({ renewBeforeHours: hours }, "Сохранение настроек автопродления…");
  }

  async function setCheckInterval(minutes: number) {
    await saveRenewalSettings({ checkIntervalMinutes: minutes }, "Сохранение настроек автопродления…");
  }

  async function toggleAutostart(enabled: boolean) {
    await run(enabled ? "Включение автозапуска…" : "Отключение автозапуска…", async () => {
      setRenewal(await invoke<RenewalStatus>("set_autostart", { enabled }));
      setMessage(enabled ? "Автозапуск iReSign включён." : "Автозапуск iReSign выключен.");
    });
  }

  async function trackApp() {
    await run("Добавление приложения в автопродление…", async () => {
      setRenewal(await invoke<RenewalStatus>("track_current_app"));
      setMessage("Приложение добавлено в автопродление.");
    });
  }

  async function untrackApp(id: string) {
    await run("Удаление приложения из автопродления…", async () => {
      setRenewal(await invoke<RenewalStatus>("untrack_app", { id }));
      setMessage("Приложение убрано из автопродления.");
    });
  }

  async function setTrackedEnabled(id: string, enabled: boolean) {
    await run("Сохранение настройки приложения…", async () => {
      setRenewal(await invoke<RenewalStatus>("set_tracked_app_enabled", { id, enabled }));
      setMessage(enabled ? "Автопродление приложения включено." : "Автопродление приложения выключено.");
    });
  }

  async function renewApp(id: string) {
    await run("Продление выполняется… Не отключайте iPhone.", async () => {
      try {
        setRenewal(await invoke<RenewalStatus>("renew_now", { id }));
        setMessage("Продление завершено.");
      } catch (value) {
        await refreshRenewal();
        throw value;
      }
    });
  }

  async function checkRenewals() {
    await run("Запрос проверки автопродления…", async () => {
      setRenewal(await invoke<RenewalStatus>("check_renewals_now"));
      setMessage("Проверка будет выполнена в ближайшее время.");
    });
  }

  async function resetRenewal() {
    await run("Сброс списка автопродления…", async () => {
      setRenewal(await invoke<RenewalStatus>("reset_renewal_plan"));
      setMessage("Список автопродления сброшен.");
    });
  }

  const pendingPhase = pendingInstall?.phase === "unknown" ? "Результат неизвестен"
    : pendingInstall?.phase === "success_recorded_pending_release" ? "Отчёт о завершении сохранён; требуется проверка"
    : "Начата ранее; результат не подтверждён";

  const disabled = !native || busy;
  const sessionActive = loggedIn || session.status === "active";
  const sessionRestoring = !loggedIn && session.status === "restoring";
  const sessionFailed = !loggedIn && session.status === "failed";
  const showLoginForm = !sessionActive && !sessionRestoring;
  const accountEmail = session.email ?? (email.trim() ? email.trim() : null);
  const schedulerTone = renewal.scheduler === "error" ? "red"
    : renewal.scheduler === "inactive" ? ""
    : ["session_required", "device_unavailable", "blocked"].includes(renewal.scheduler) ? "amber"
    : "green";
  const schedulerText = `${renewal.schedulerMessage || "Автопродление неактивно."}${renewal.scheduler === "renewing" && renewal.currentApp ? ` — ${renewal.currentApp}` : ""}`;
  const nextCheck = formatTimestamp(renewal.nextCheckAt);

  return <main>
    <header><div className="mark" aria-hidden="true">iR</div><div><h1>iReSign</h1><p>Подпись и автопродление приложений для iPhone</p></div><span className="badge">Прототип 0.1</span></header>
    {!native && <div className="notice">Это просмотр интерфейса. Подключение iPhone и подпись доступны в Windows-приложении.</div>}
    <div className="storage"><span className={protectedStorage ? "dot green" : "dot"} />
      {protectedStorage === null ? "Проверка защищённого хранилища…" : protectedStorage ? "Секреты подписи: защищённое хранилище Windows" : "Защищённое хранилище недоступно — вход отключён"}</div>

    <section><div className="section-heading"><div><span className="number">1</span><h2>iPhone</h2></div><button className="secondary" disabled={disabled} onClick={() => void refresh()}>Обновить список</button></div>
      <p className="hint">Подключите по USB, разблокируйте и подтвердите доверие компьютеру.</p>
      <label>Устройство<select value={selected} disabled={disabled || !devices.length} onChange={event => void chooseDevice(event.target.value)}>
        <option value="" disabled>Выберите подключённый iPhone</option>
        {devices.map(device => <option key={device.udid} value={device.udid}>{device.name} · iOS {device.version} · {device.connectionType}</option>)}
      </select></label>
    </section>

    <section><div className="section-heading"><div><span className="number">2</span><h2>Приложение</h2></div><button className="secondary" disabled={disabled} onClick={() => void chooseIpa()}>Выбрать IPA</button></div>
      {ipa ? <div className="ipa"><strong>{ipa.name}</strong><span>Версия {ipa.version || "не указана"}</span><code>{ipa.bundleId}</code><small title={ipa.sha256}>SHA-256: {ipa.sha256.slice(0, 20)}…</small></div> : <p className="empty">Выберите локальный IPA. Сначала проверим архив и сведения о приложении.</p>}
    </section>

    <section><div className="section-heading"><div><span className="number">3</span><h2>Apple Account</h2></div>{sessionActive && <span className="badge success">Вход выполнен</span>}</div>
      {sessionRestoring ? <div className="restoring"><span className="spinner" aria-hidden="true" /><span>Восстановление сохранённой сессии Apple Account…</span></div>
        : sessionActive ? <div className="account">
          {accountEmail && <p className="account-email">{accountEmail}</p>}
          <p>{session.stored
            ? "Сессия сохранена в защищённом хранилище Windows и будет восстановлена после перезапуска."
            : "Сессия действует только в текущем запуске. Для автопродления после перезапуска сохраните её при следующем входе."}</p>
          <div className="account-actions"><button className="secondary" disabled={disabled} onClick={() => void logout()}>Выйти</button></div>
        </div>
        : <>
          {sessionFailed && <div className="alert" role="alert">Сессия Apple Account не активна. Войдите снова.</div>}
          {sessionFailed && session.stored && <div className="account-recovery"><button className="secondary" disabled={disabled} onClick={() => void forgetSession()}>Забыть сохранённую сессию</button></div>}
          {showLoginForm && <form onSubmit={event => void login(event)} autoComplete="off">
            <label>Свой Anisette-сервис<input type="url" placeholder="https://ваш-сервис или http://127.0.0.1:6969" value={anisette} onChange={event => setAnisette(event.target.value)} disabled={disabled} required /></label>
            <div className="fields"><label>Apple Account<input type="email" autoComplete="off" value={email} onChange={event => setEmail(event.target.value)} disabled={disabled} required /></label>
              <label>Пароль<input type="password" autoComplete="off" value={password} onChange={event => setPassword(event.target.value)} disabled={disabled} required /></label></div>
            <label className="check"><input type="checkbox" checked={persistSession} disabled={disabled} onChange={event => setPersistSession(event.target.checked)} /><span>Сохранить сессию для автопродления</span></label>
            <p className="hint">Пароль не сохраняется. Сессия хранится в Windows Credential Manager и нужна для фонового продления после перезапуска. Локальный Anisette разрешён только по loopback: 127.0.0.1 или localhost.</p>
            <button className="secondary" type="submit" disabled={disabled || !protectedStorage || !eventsReady}>Войти</button>
          </form>}
        </>}
    </section>

    <div className={error ? "status error" : "status"} role={error ? "alert" : "status"}>{busy && <span className="spinner" aria-hidden="true" />}{message || "Выберите iPhone и IPA для первой установки."}</div>
    {pendingInstall && <section aria-labelledby="pending-install-title">
      <h2 id="pending-install-title">Проверьте предыдущую установку</h2>
      <p>{pendingPhase}. Автоматический повтор заблокирован.</p>
      <div className="ipa"><strong>{pendingInstall.bundleId}</strong><code>iPhone: {pendingInstall.targetDevice}</code>
        <small>Операция: {pendingInstall.effectId}</small><small title={pendingInstall.artifactSha256}>SHA-256: {pendingInstall.artifactSha256.slice(0, 20)}…</small></div>
      <p className="hint">Проверьте приложение на этом iPhone. Если установка ещё продолжается, дождитесь её завершения. Кнопка ниже сохраняет ваше решение разрешить отдельную попытку; она не подтверждает успешную установку и не запускает новую.</p>
      <button className="secondary" disabled={disabled || pendingInstall.targetDevice !== selected} onClick={() => void resolveInstall()}>Проверил приложение на iPhone — разрешить повтор</button>
    </section>}
    <button className="primary" disabled={disabled || !selected || !ipa || !loggedIn || pendingInstall !== null} onClick={() => void install()}>{busy ? "Выполняется…" : "Подписать и установить"}</button>

    <section aria-labelledby="renewal-title"><div className="section-heading">
      <div><span className="number">4</span><h2 id="renewal-title">Автопродление</h2></div>
      <label className="toggle"><input type="checkbox" aria-label="Автопродление" checked={renewal.enabled} disabled={disabled} onChange={event => void setRenewalEnabled(event.target.checked)} /><span>{renewal.enabled ? "Включено" : "Выключено"}</span></label>
    </div>
      <p className="hint">iReSign проверяет срок профиля, пока приложение запущено и iPhone подключён по USB. Сессия Apple Account должна быть сохранена или действовать в текущем запуске.</p>
      <div className="settings-grid">
        <label>Продлевать за<select value={renewal.renewBeforeHours} disabled={disabled} onChange={event => void setRenewBefore(Number(event.target.value))}>
          {[24, 48, 72, 120].map(hours => <option key={hours} value={hours}>{hours} ч</option>)}
        </select></label>
        <label>Проверять<select value={renewal.checkIntervalMinutes} disabled={disabled} onChange={event => void setCheckInterval(Number(event.target.value))}>
          {[5, 15, 30, 60].map(minutes => <option key={minutes} value={minutes}>{minutes} мин</option>)}
        </select></label>
      </div>
      <label className="check"><input type="checkbox" checked={renewal.autostart} disabled={disabled} onChange={event => void toggleAutostart(event.target.checked)} /><span>Запускать iReSign при входе в Windows</span></label>
      <div className="renewal-actions"><button className="secondary" disabled={disabled || !ipa || !selected || !loggedIn} onClick={() => void trackApp()}>Добавить текущее приложение</button></div>
      <div className="scheduler">
        <div className="scheduler-text"><span className={schedulerTone ? `dot ${schedulerTone}` : "dot"} aria-hidden="true" /><div>
          <p>{schedulerText}</p>
          {nextCheck && <small>Следующая проверка: {nextCheck}</small>}
        </div></div>
        <button className="secondary compact" disabled={disabled || renewal.scheduler === "renewing"} onClick={() => void checkRenewals()}>Проверить сейчас</button>
      </div>
      {renewal.scheduler === "error" && <div className="warning">
        <p>Планировщик автопродления сообщает об ошибке. Сбросьте список, чтобы начать заново.</p>
        <button className="secondary compact" disabled={disabled} onClick={() => void resetRenewal()}>Сбросить список</button>
      </div>}
      <div className="tracked-list">
        {renewal.apps.length === 0
          ? <p className="empty">Пока нет приложений. Установите IPA, выберите iPhone и войдите, затем добавьте приложение.</p>
          : renewal.apps.map(app => {
            const expires = formatTimestamp(app.profileExpiresAt);
            const renewed = formatTimestamp(app.lastRenewedAt);
            return <article className="tracked" key={app.id}>
              <div className="tracked-head">
                <div><strong>{app.name || app.bundleId}</strong><code>{app.bundleId}</code></div>
                <label className="check"><input type="checkbox" checked={app.enabled} disabled={disabled} onChange={event => void setTrackedEnabled(app.id, event.target.checked)} /><span>Продлевать</span></label>
              </div>
              <div className="tracked-meta">
                <span>iPhone: {app.deviceUdid.slice(0, 8)}…</span>
                <span>Профиль до: {expires ?? "срок неизвестен"}</span>
                <span>Последнее продление: {renewed ?? "ещё не выполнялось"}</span>
              </div>
              <Outcome app={app} />
              <div className="tracked-actions">
                <button className="secondary compact" disabled={disabled} onClick={() => void renewApp(app.id)}>Продлить сейчас</button>
                <button className="secondary compact" disabled={disabled} onClick={() => void untrackApp(app.id)}>Убрать</button>
              </div>
            </article>;
          })}
      </div>
    </section>
    <footer>Автопродление работает, пока приложение запущено и iPhone подключён по USB. Основа: iloader / isideload, MIT.</footer>

    {challenge && <div className="overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="tfa-title"><h2 id="tfa-title">Подтверждение Apple</h2><p>Введите шестизначный код с доверенного устройства или из SMS.</p>
      <form onSubmit={event => void submitCode(event)}><label>Код<input autoFocus inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={event => setCode(event.target.value.replace(/\D/g, ""))} autoComplete="one-time-code" required /></label>
        <div className="modal-actions"><button type="button" className="secondary" onClick={() => { void invoke("submit_two_factor", { code: "" }).catch(() => {}); setChallenge(false); setCode(""); }}>Отмена</button><button className="primary" disabled={code.length !== 6}>Подтвердить</button></div></form></div></div>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
