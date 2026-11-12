# RemonteVoice: переименование и совместимость

12 ноября 2026 проект **RemoteVoice** переименован в **RemonteVoice**, а
отдельный подписчик — в **iReSign**. Это изменение идентичностей, а не только
заголовков. Совместимость со старыми установками решается отдельно.

## Новые идентификаторы

| Что | Было | Стало |
|---|---|---|
| Основное приложение | RemoteVoice | RemonteVoice |
| iOS bundle ID | `dev.remotevoice.ios` | `dev.remontevoice.ios` |
| Android applicationId | `dev.remotevoice.android` | `dev.remontevoice.android` |
| Android debug suffix | `.debug` | `.debug` (без изменений) |
| URL-схема сопряжения | `remotevoice://` | `remontevoice://` |
| Python-пакет Host | `remotevoice` | `remontevoice` |
| Windows EXE | `RemoteVoice.exe` | `RemonteVoice.exe` |
| Broker EXE | `RemoteVoiceBroker.exe` | `RemonteVoiceBroker.exe` |
| Windows-пакет | `RemoteVoicePair` | `RemonteVoicePair` |
| Профиль Windows | `%APPDATA%\RemoteVoice` | `%APPDATA%\RemonteVoice` |
| Установка | `%LOCALAPPDATA%\RemoteVoice\app` | `%LOCALAPPDATA%\RemonteVoice\app` |
| Драйвер/служба | `RemoteVoiceAudio` | `RemonteVoiceAudio` |
| Android release env | `REMOTEVOICE_RELEASE_*` | `REMONTEVOICE_RELEASE_*` |
| Подписчик | LocalSign → Remonte (ошибочно) | iReSign |
| iReSign identifier | — | `dev.remontevoice.iresign` |

## Что это ломает

- **iOS**: новое приложение с новым bundle ID. Старый RemoteVoice останется
  установленным отдельно; данные, Keychain, UserDefaults и pairing не
  переносятся. Новый IPA нужно собрать из переименованного проекта.
- **Android**: новый applicationId — отдельное приложение. Старый debug/release
  APK не обновится поверх нового. Сопряжение и настройки не переносятся.
- **Windows Host**: новый профиль `%APPDATA%\RemonteVoice`; старые настройки,
  TLS identity и pairing не читаются. Новый EXE/пакет и новые ярлыки; старая
  установка остаётся отдельной.
- **Сопряжение**: старые QR-коды и ссылки `remotevoice://` недействительны.
  Телефон и Host нужно сопрячь заново.
- **Драйвер**: имя службы/пакета изменилось. Старый драйвер не становится новым
  автоматически; установка нового требует отдельного шага и прав администратора.
- **CI**: имена секретов/переменных `REMONTEVOICE_*` нужно завести в GitHub;
  старые `REMOTEVOICE_*` больше не читаются.
- **Исторические артефакты**: каталоги `host/build/...` и `android/preview/...`
  сохранили прежние имена и внутренние идентичности. Ссылки на них в
  исторических разделах документации могут указывать на старые имена.
- **iReSign**: отдельное приложение; его сессия, сертификаты и Anisette не
  связаны с RemonteVoice. Старые пространства LocalSign/Remonte не читаются.

## Что проверено после переименования

- GitHub Actions: оба workflow завершились успешно; macOS-раннер собрал
  симулятор, unsigned iPhone app и IPA `RemonteVoice-iPhone-unsigned.ipa`
  (bundle ID `dev.remontevoice.ios`, SHA-256
  `b23e9080d17519e3d97ba79fa3eae9ee09eb574155b16091b5d5f9d41f5918fa`).
- Host: 939 Python-тестов, 0 ошибок; Windows-артефакт `RemonteVoice-Windows`
  собран на CI.
- Android: debug-сборка, 163 JVM-теста, lint — 21 предупреждение, без ошибок;
  CI собрал unsigned release-артефакт.
- iOS: source-inspection checks прошли; CI выполнил XcodeGen, Xcode build и
  упаковку IPA.
- Host PyInstaller: локально собран `host/build/dist/RemonteVoicePair`.
- iReSign: 39 Rust-тестов, Clippy, frontend build, EXE, Anisette provisioning.
- Реальная установка на iPhone/Android, новый драйвер и миграция старых данных
  не проверялись.

## Ближайший порядок совместимости

1. Собрать новый iOS IPA из `ios/project.yml` с `dev.remontevoice.ios`.
2. Установить новый Android APK и новое Windows-приложение; сопрячь заново.
3. Проверить драйвер `RemonteVoiceAudio` и возврат аудиоустройств.
4. Решить, нужен ли перенос старых настроек; автоматический перенос не обещан.
