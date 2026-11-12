# Сборка Windows-прототипа

Необходимы Node.js/npm, Rust MSVC и Visual Studio C++ Build Tools (Windows SDK,
MSVC, CMake). Если зависимостям нужен Perl, сценарий использует имеющийся Perl
из Git for Windows. Системные инструменты сценарий не устанавливает.

Из корня проекта:

```powershell
npm.cmd ci --ignore-scripts --prefix signer/app
powershell -NoProfile -File signer/scripts/build.ps1 -DebugBuild
```

Для release-сборки убрать `-DebugBuild`. Прототип пока не создаёт установщик,
MSIX, Store-пакет или PE-подпись издателя.

При наличии `signer/.tools/cargo` и `signer/.tools/rustup` сценарий использует
этот локальный Rust. Такой каталог не хранится в Git. PATH меняется только
для выполняющего сборку процесса.

После успешной сборки:

```powershell
signer/app/src-tauri/target/debug/iresign.exe --diagnostics
signer/app/src-tauri/target/debug/iresign.exe --check-frontend
signer/app/src-tauri/target/debug/iresign.exe --check-ipa "RemonteVoice-Install/RemonteVoice-iPhone-unsigned.ipa"
signer/app/src-tauri/target/debug/iresign.exe --check-renewal
signer/app/src-tauri/target/debug/iresign.exe --check-session
signer/app/src-tauri/target/debug/iresign.exe --check-session-storage
signer/app/src-tauri/target/debug/iresign.exe --check-anisette http://127.0.0.1:6969
```

`--check-frontend` проверяет наличие встроенных HTML/JS/CSS и настройку
приватного WebView. `--check-ipa` разбирает локальный архив. `--check-renewal`
читает список автопродления и печатает только настройки, bundle ID, хеш UDID и
сроки. `--check-session` проверяет только наличие сохранённой сессии.
`--check-session-storage` записывает фиктивную не-Apple сессию в отдельное
пространство `dev.remontevoice.iresign.v1.selftest` в Windows Credential
Manager, проверяет чтение и удаляет её; реальные токены и основная сессия не
затрагиваются. `--check-anisette` проверяет доступность Anisette и при
необходимости выполняет провижининг; Apple Account для этого не нужен.

Эти команды не выполняют вход в Apple Account, создание сертификата, подпись,
установку или продление приложения. Первая проверяет подключение устройства и
хранилище, остальные — локальные файлы, защищённое хранилище и Anisette.

Автозапуск включается переключателем в интерфейсе и пишет значение `iReSign`
в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. При запуске с
`--autostart` окно сворачивается. Автозапуск не устанавливает службу и не
работает без запущенного процесса приложения.

Для компонентных проверок в настроенной среде MSVC:

```powershell
cargo test --manifest-path signer/app/src-tauri/Cargo.toml --locked
cargo clippy --manifest-path signer/app/src-tauri/Cargo.toml --all-targets -- -D warnings
```

Текущая сборка: 39 Rust-тестов, Clippy без предупреждений, `tsc --noEmit` и
Vite production build. Это не заменяет проверку реальной подписи и продления.
