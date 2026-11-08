import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import "./style.css";

type Device = { name: string; udid: string; version: string; connectionType: string };
type Ipa = { name: string; bundleId: string; version: string; sha256: string };
type Failure = { code: string; message: string };
type PendingInstall = { effectId: string; artifactSha256: string; targetDevice: string; bundleId: string; phase: string };
const native = "__TAURI_INTERNALS__" in window;

function failureMessage(value: unknown): string {
  if (value && typeof value === "object" && "code" in value && "message" in value) {
    return String((value as Failure).message);
  }
  return "Операция не выполнена. Повторите после проверки подключения.";
}

function App() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState("");
  const [ipa, setIpa] = useState<Ipa | null>(null);
  const [protectedStorage, setProtectedStorage] = useState<boolean | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [anisette, setAnisette] = useState("");
  const [loggedIn, setLoggedIn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState(false);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  const [eventsReady, setEventsReady] = useState(false);
  const [pendingInstall, setPendingInstall] = useState<PendingInstall | null>(null);

  useEffect(() => {
    if (!native) return;
    let disposed = false;
    const unlisteners: UnlistenFn[] = [];
    void (async () => {
      try {
        for (const [event, handler] of [
          ["two-factor-required", () => { setCode(""); setChallenge(true); }],
          ["two-factor-closed", () => { setCode(""); setChallenge(false); }],
        ] as const) {
          const unlisten = await listen(event, handler);
          if (disposed) unlisten(); else unlisteners.push(unlisten);
        }
        if (!disposed) setEventsReady(true);
        const available = await invoke<boolean>("protected_storage_available");
        if (!disposed) setProtectedStorage(available);
        try {
          const found = await invoke<Device[]>("list_devices");
          if (!disposed) {
            setDevices(found);
            setMessage(found.length ? "iPhone обнаружен. Выберите устройство." : "Подключите iPhone по USB и разблокируйте его.");
          }
        } catch (value) {
          if (!disposed) { setError(true); setMessage(failureMessage(value)); }
        }
      } catch {
        if (!disposed) { setError(true); setMessage("Не удалось инициализировать приложение. Перезапустите LocalSign."); }
      }
    })();
    return () => { disposed = true; unlisteners.forEach(fn => fn()); };
  }, []);

  async function run(label: string, action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(false); setMessage(label);
    try { await action(); }
    catch (value) { setError(true); setMessage(failureMessage(value)); }
    finally { setBusy(false); }
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
      await invoke("login_new", { email, password: value, anisetteServer: anisette });
      setLoggedIn(true); setMessage("Вход выполнен. Можно подписать и установить выбранный IPA.");
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
        setMessage("Установка завершена. Проверьте запуск приложения на iPhone; при необходимости включите режим разработчика и доверие профилю.");
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

  const pendingPhase = pendingInstall?.phase === "unknown" ? "Результат неизвестен"
    : pendingInstall?.phase === "success_recorded_pending_release" ? "Отчёт о завершении сохранён; требуется проверка"
    : "Начата ранее; результат не подтверждён";

  const disabled = !native || busy;
  return <main>
    <header><div className="mark" aria-hidden="true">L</div><div><h1>LocalSign</h1><p>Личная подпись приложений для iPhone</p></div><span className="badge">Прототип 0.1</span></header>
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

    <section><div className="section-heading"><div><span className="number">3</span><h2>Apple Account</h2></div>{loggedIn && <span className="badge success">Вход выполнен</span>}</div>
      {loggedIn ? <div className="signed-in"><p>Сессия действует в текущем запуске приложения.</p><button className="secondary" disabled={disabled} onClick={() => void run("Завершение сессии…", async () => { await invoke("logout"); setLoggedIn(false); setMessage("Сессия завершена."); })}>Выйти</button></div> : <form onSubmit={event => void login(event)} autoComplete="off">
        <label>Свой Anisette-сервис<input type="url" placeholder="https://ваш-сервис" value={anisette} onChange={event => setAnisette(event.target.value)} disabled={disabled} required /></label>
        <div className="fields"><label>Apple Account<input type="email" autoComplete="off" value={email} onChange={event => setEmail(event.target.value)} disabled={disabled} required /></label>
          <label>Пароль<input type="password" autoComplete="off" value={password} onChange={event => setPassword(event.target.value)} disabled={disabled} required /></label></div>
        <p className="hint">Пароль не сохраняется. Сервис Anisette пока нужно настроить отдельно. Реальная подпись этой сборкой ещё не проверена.</p>
        <button className="secondary" type="submit" disabled={disabled || !protectedStorage || !eventsReady}>Войти</button>
      </form>}
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
    <footer>Автопродление на Windows — следующий этап после проверки установки. Основа: iloader / isideload, MIT.</footer>

    {challenge && <div className="overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="tfa-title"><h2 id="tfa-title">Подтверждение Apple</h2><p>Введите шестизначный код с доверенного устройства или из SMS.</p>
      <form onSubmit={event => void submitCode(event)}><label>Код<input autoFocus inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={event => setCode(event.target.value.replace(/\D/g, ""))} autoComplete="one-time-code" required /></label>
        <div className="modal-actions"><button type="button" className="secondary" onClick={() => { void invoke("submit_two_factor", { code: "" }).catch(() => {}); setChallenge(false); setCode(""); }}>Отмена</button><button className="primary" disabled={code.length !== 6}>Подтвердить</button></div></form></div></div>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
