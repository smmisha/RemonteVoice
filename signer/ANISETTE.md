# Локальный Anisette для iReSign

Anisette-сервис нужен для входа в Apple Account и подписи. На этом ноутбуке он
поднят локально в Ubuntu WSL, потому что Docker здесь не установлен.

## Что и где

- Сервис: [Dadoum/anisette-v3-server](https://github.com/Dadoum/anisette-v3-server),
  версия `2.2.2`, коммит `684895737aaf1a1c4586fcd5b67509ffd500a36c`.
- Сборка: `ldc2` + `dub`, статическая конфигурация, каталог
  `/opt/anisette-v3-server` в Ubuntu WSL.
- Данные: `/opt/anisette-v3-data` (устройство, библиотеки Apple Music).
- Журнал: `/var/log/anisette-v3.log`.
- Адрес для iReSign: `http://127.0.0.1:6969`.
- Слушает `0.0.0.0:6969` внутри WSL; Windows обращается через loopback.
  Наружу в локальную сеть WSL по умолчанию не публикует.

## Управление

Запуск вручную:

```powershell
wsl.exe -d Ubuntu -u root -- /opt/anisette-v3-server/start-iresign.sh
```

Остановка:

```powershell
wsl.exe -d Ubuntu -u root -- pkill -x anisette-v3-ser
```

Проверка из iReSign (заодно выполняет провижининг, если нужно):

```powershell
.\signer\preview\iReSign.exe --check-anisette http://127.0.0.1:6969
```

Ожидается `{"provisioned":true,"reachable":true}`.

## Автозапуск

При входе в Windows запускается ярлык:

`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\iReSign Anisette.vbs`

Он скрыто вызывает
`%LOCALAPPDATA%\iReSign\start-anisette.ps1`, а тот — WSL-скрипт
`/opt/anisette-v3-server/start-iresign.sh`. Скрипт идемпотентен: если сервис уже
работает, второй экземпляр не запускается.

Удалить автозапуск — удалить файл `iReSign Anisette.vbs` из папки Startup.

## Безопасность и границы

- Пароль Apple Account в Anisette не передаётся. Сервис получает только
  технические данные Anisette, необходимые Apple.
- Запросы идут по HTTP только по loopback (`127.0.0.1`/`localhost`). Удалённый
  Anisette в iReSign допускается только по HTTPS.
- Сервис запущен от root внутри WSL. Это локальная лабораторная установка, не
  готовая к публичному хостингу.
- Если WSL или дистрибутив Ubuntu удалить, сервис и его данные исчезнут.

## Лицензия

В репозитории `anisette-v3-server` **не заявлена лицензия**. Поэтому он
используется только локально для личной проверки. Встраивать, перепродавать или
распространять его вместе с iReSign нельзя без разрешения автора и проверки
лицензий зависимостей (`vibe-d`, `provision`, `slf4d` и другие). Для публичного
продукта нужен собственный или явно лицензированный Anisette.

## Docker

Docker на этом ноутбуке не найден: ни в `PATH`, ни в `Program Files`, ни в WSL.
Если он появится, можно использовать образ `dadoum/anisette-v3-server` с той же
оговоркой о лицензии; локальная WSL-сборка уже работает.

Проверки этой установки: [anisette-local-verification.json](../.harness/evidence/signing-utility/anisette-local-verification.json).
