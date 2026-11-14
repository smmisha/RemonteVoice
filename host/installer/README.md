# RemonteVoice: установочный пакет (Windows)

Пакет ставит приложение как продукт: защищённая установка в `Program Files`,
привилегированная задача broker'а, ярлыки и запись в «Программы и компоненты».
Версия пакета берётся из `host/remontevoice/deployment.py` (`VERSION`).

## Сборка

```powershell
# одной командой: собрать пару (PyInstaller) + драйвер-пакет + скрипты + zip
powershell -NoProfile -ExecutionPolicy Bypass -File host\installer\Build-RemonteVoiceSetup.ps1

# из уже собранной пары (быстрее)
powershell -NoProfile -ExecutionPolicy Bypass -File host\installer\Build-RemonteVoiceSetup.ps1 `
    -Pair host\build\desktop-065\dist\RemonteVoicePair
```

Результат: `host\build\RemonteVoice-Setup-<version>.zip` и реквизиты сборки
`host\build\RemonteVoice-Setup-<version>.build.json` (SHA-256 архива, версия,
instance драйвера, размер). Скрипт никогда не перезаписывает существующие
выходные каталоги и архив.

## Структура пакета

```
RemonteVoice-Setup-<version>/
  app/                        RemonteVoice.exe, RemonteVoiceBroker.exe, _internal, манифест установки
  driver/                     тестово подписанный драйвер: .sys/.inf/.cat, .cer, helper создания устройства
  Install-RemonteVoice.ps1    установка (admin, UAC)
  Update-RemonteVoice.ps1     обновление/repair поверх установки
  Uninstall-RemonteVoice.ps1  удаление (admin, UAC), self-elevate из «Программ и компонент»
  common.ps1                  общие функции установки (ACL, задача, манифест, broker)
  remontevoice-task.xml.in    шаблон задачи входа (broker напрямую, HighestAvailable)
  install.cmd / uninstall.cmd запуск двойным щелчком
  package-manifest.json       версия, хеши всех файлов, сертификат драйвера
  README.txt                  инструкция для пользователя
```

Установщик сверяет SHA-256 каждого файла пакета перед установкой
(`-SkipIntegrityCheck` только для диагностики).

## Установка продукта

```powershell
# из распакованного пакета (сам запросит UAC)
powershell -ExecutionPolicy Bypass -File .\Install-RemonteVoice.ps1 -Package . -StartApp
```

Что делает установщик:

1. останавливает работающее приложение (штатное закрытие) и broker;
2. проверяет целостность пакета;
3. копирует приложение в `%ProgramFiles%\RemonteVoice`, убирает файлы, которых
   нет в новом манифесте, и выставляет защищённый ACL (owner `Administrators`,
   `Users` — только чтение/запуск);
4. определяет instance аудиодрайвера (`ROOT\MEDIA\NNNN`) и, если он отличается от
   запечённого в манифесте, переписывает `driver_instance_id`;
5. копирует скрипты обслуживания в `%ProgramData%\RemonteVoice\installer`;
6. регистрирует задачу входа `RemonteVoice` (broker напрямую, `HighestAvailable`,
   без консольного окна), запускает её и ждёт pipe `RemonteVoice-Mic-*`;
7. создаёт ярлыки (рабочий стол, меню «Пуск») и запись удаления в HKLM;
8. пишет состояние `%ProgramData%\RemonteVoice\install-state.json` и квитанцию
   `%ProgramData%\RemonteVoice\receipts\install.json`;
9. с `-StartApp` запускает приложение через оболочку Explorer (обычный токен).

Драйвер. Если устройство уже установлено, установщик просто использует его
instance. Установка драйвера — отдельный dev-режим:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-RemonteVoice.ps1 -Package . -InstallDriver
```

Она требует выключенного Secure Boot, включает `testsigning` (перезагрузка),
импортирует тестовый сертификат в `LocalMachine\Root` и `TrustedPublisher` и
создаёт root-устройство через `driver\RemonteVoiceDriverSetup.exe` (SetupAPI).
Это development-подпись, а не production-поставка драйвера.

## Обновление

```powershell
powershell -ExecutionPolicy Bypass -File .\Update-RemonteVoice.ps1 -Package .\RemonteVoice-Setup-<next>.zip
```

Требует более новую версию (или `-AllowSameVersion` для repair), сохраняет
настройки и сопряжение в `%APPDATA%\RemonteVoice`, перезапускает broker и, если
приложение было открыто, запускает его снова.

## Удаление

```powershell
powershell -ExecutionPolicy Bypass -File .\Uninstall-RemonteVoice.ps1
```

Удаляет файлы, задачу, ярлыки и запись в реестре; настройки сохраняются
(`-RemoveConfig` удаляет их, `-RemoveDriver` удаляет драйвер и выключает
`testsigning`, `-KeepLogs` сохраняет журналы).

## Приёмка на этой машине

```powershell
host\installer\acceptance\Run-Acceptance.cmd
```

Цикл install → update (repair) → uninstall → final install с проверками:
набор файлов и instance манифеста, owner и ACL, action задачи, живой broker с
одним pipe и без окна, ярлыки, запись удаления, состояние, сохранение настроек,
удаление без остатков и неприкосновенность dev-установки. Скрипт берёт самый
свежий архив `RemonteVoice-Setup-*.zip` из `host\build` и требует одно
подтверждение UAC. Квитанция: `host\build\package-acceptance\acceptance.json`.

## Границы

- Пакет не подписан: это development-сборка. Production-подпись, WHCP и
  поставка драйвера — открытые требования R6.
- Требуется Windows 10/11 x64 и администратор для установки.
- `RemonteVoice-Dev` (старая dev-установка) пакетом не управляется; после
  приёмки её можно удалить вручную.
