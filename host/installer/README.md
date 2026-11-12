# RemonteVoice: установка, обновление, удаление

Скрипты ставят приложение в профиль пользователя (без прав администратора):
`%LOCALAPPDATA%\RemoteVoice\app`, создают ярлыки на рабочем столе и в меню «Пуск»
и запись «Программы и компоненты» для удаления. Настройки и сопряжение лежат
в `%APPDATA%\RemoteVoice` и при обновлении/удалении не теряются.

## Установка

```powershell
# из папки с RemoteVoice.exe и _internal
powershell -ExecutionPolicy Bypass -File .\Install-RemoteVoice.ps1 -Package .\RemonteVoice

# или из zip-пакета
powershell -ExecutionPolicy Bypass -File .\Install-RemoteVoice.ps1 -Package .\RemonteVoice-Windows-0.1.40-local.zip
```

Параметры: `-InstallRoot <путь>` (по умолчанию `%LOCALAPPDATA%\RemoteVoice\app`),
`-NoShortcuts`, `-NoRegistry` — для тихой/тестовой установки.

## Обновление

```powershell
powershell -ExecutionPolicy Bypass -File .\Update-RemoteVoice.ps1 -Package .\RemonteVoice-Windows-0.1.40-local.zip
```

Скрипт останавливает запущенное приложение, заменяет файлы и запускает его снова.
Конфигурация в `%APPDATA%\RemoteVoice` сохраняется.

## Удаление

```powershell
powershell -ExecutionPolicy Bypass -File .\Uninstall-RemoteVoice.ps1
```

Удаляет приложение, ярлыки и запись в реестре. Настройки в `%APPDATA%\RemoteVoice`
остаются; добавьте `-RemoveConfig`, чтобы удалить и их.
