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
signer/app/src-tauri/target/debug/localsign.exe --diagnostics
signer/app/src-tauri/target/debug/localsign.exe --check-frontend
signer/app/src-tauri/target/debug/localsign.exe --check-ipa "RemoteVoice-Install/RemoteVoice-iPhone-unsigned.ipa"
```

Команда `--check-frontend` проверяет наличие встроенных HTML/JS/CSS и настройку
приватного WebView. Она не доказывает отображение окна или полный GUI-сценарий.

Эти команды не выполняют вход в Apple Account, создание сертификата, подпись
или установку приложения. Первая проверяет подключение устройства и хранилище,
вторая — локальный архив. Установка и запуск на iPhone проверяются отдельно.

Для компонентных проверок в настроенной среде MSVC:

```powershell
cargo test --manifest-path signer/app/src-tauri/Cargo.toml --locked
```
