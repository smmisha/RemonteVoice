# Срочный аудит RemoteVoice

Исторический срез до исправлений. Текущий статус и результаты последующей работы — в [STABILITY.md](STABILITY.md); формулировка «открыто» ниже относится к моменту аудита.

**23 октября 2026, Europe/Kiev.** Рабочий корень: `M:/Projects/RemonteVoice`.

## Итог

Выявлены **19 открытых находок: 7 P1 и 12 P2**. Семь дефектов воспроизведены безопасными пробами, семь подтверждены путями исходного кода, пять относятся к конфигурации поставки и доказательств. Четыре P1 находятся в ещё не принятом на устройстве iPhone-клиенте; три — в Host/сопряжении.

P1 — приоритет до расширения соответствующего компонента: возможны нарушение владения устройствами, потеря управляемой остановки/сопряжения либо ошибки границы и жизненного цикла микрофона. P2 — следующий слой надёжности, протокола и поставки.

Единая фактическая и целевая архитектура: [ARCHITECTURE.md](ARCHITECTURE.md). Полный реестр: [.harness/evidence/audit/findings.json](.harness/evidence/audit/findings.json). Все перечисленные ошибки остаются открытыми; аудит не является их исправлением.

## Реестр

| ID | Приоритет | Область | Ошибка | Доказательство |
| --- | --- | --- | --- | --- |
| A01 | P1 | Host / ownership | Второй Host отбирает маршруты действующей сессии | Воспроизведено |
| A02 | P1 | Host / workers | Остановка теряет ссылки на ещё живые аудиопотоки | Воспроизведено |
| A03 | P1 | Pairing / session slot | Отказ busy потребляет PIN и заменяет токен без передачи клиенту | Воспроизведено |
| A04 | P1 | iPhone / LAN boundary | Проверка IPv4 пропускает внешний DNS-адрес | Путь подтверждён кодом |
| A05 | P1 | iPhone / send and mute | Микрофон отправляет кадры без ограниченной очереди и ожидания ACK | Путь подтверждён кодом |
| A06 | P1 | iPhone / audio transaction | Ошибка включения микрофона не откатывает аудиодвижок | Путь подтверждён кодом |
| A07 | P1 | iPhone / generation | Поздние Task и permission callbacks меняют новый сеанс | Путь подтверждён кодом |
| A08 | P2 | Host / route transaction | Ошибка вывода оставляет частично активированный сеанс | Воспроизведено |
| A09 | P2 | Android / preview protocol | Поздний ответ экрана принимается за ответ нового запроса | Воспроизведено |
| A10 | P2 | Pairing / validation | Unicode credential вызывает необработанный TypeError | Воспроизведено |
| A11 | P2 | PCM / validation | NaN проходит протокол и достигает аудиоадаптера | Воспроизведено |
| A12 | P2 | iPhone / cleanup | Ошибка соединения не инвалидирует URLSession | Путь подтверждён кодом |
| A13 | P2 | Delivery / APK signing | Чистые CI-сборки не сохраняют подпись обновления APK | Наблюдаемая конфигурация |
| A14 | P2 | Harness / freshness | Fingerprint не включает конфигурации, которые читают тесты | Наблюдаемая конфигурация |
| A15 | P2 | Deployment / identity | Рабочий Host запускается диагностическим wrapper без build manifest | Наблюдаемая конфигурация |
| A16 | P2 | Delivery / baseline | Текущее локальное дерево не имеет Git baseline | Наблюдаемая конфигурация |
| A17 | P2 | Harness / scope | Активные разрешения и решения противоречат новым фактам | Наблюдаемая конфигурация |
| A18 | P2 | LocalSign / artifact identity | Проверка hash не связывает байты, которые будут подписаны | Путь подтверждён кодом |
| A19 | P2 | LocalSign / operation recovery | Нет общей политики дедлайнов и восстановления неизвестной установки | Путь подтверждён кодом |

## Порядок исправления и общая причина

1. **Владелец сеанса:** A01/A02/A03/A08. Блокировка процесса до любых восстановлений, атомарное резервирование слота/токена, транзакция всех устройств и подтверждённая остановка работников. Это первый срез для текущего Windows/Android сценария.
2. **Клиенты и протокол:** iPhone A04/A05/A06/A07/A12 — до установки на iPhone; Android preview A09 и входные данные A10/A11 — отдельными ограниченными патчами с негативными сценариями.
3. **Поставка и проверяемость:** A13/A14/A15/A16/A17. Стабильная подпись APK, полный input manifest, одна точка запуска, build/deployment identity и восстанавливаемый исходный baseline.
4. **LocalSign:** A18/A19 — неизменяемый входной IPA и журнал внешнего эффекта. Реальная подпись/Anisette/автопродление остаются отдельной приёмкой.

Повторяющийся архитектурный дефект — ответственность за один сеанс распределена между UI, сокетом, работниками и адаптерами устройств без общего владельца и общей транзакции. Целевая схема: UI → SessionCoordinator → RouteTransaction + WorkerSupervisor + Transport/Preview. Первые исправления возможны в существующих файлах без массового переименования или переписывания приложения.

## Детали и приёмка

### A01 — Второй Host отбирает маршруты действующей сессии

**P1 · Воспроизведено · исправлено в коде, сверено 2026-11-08.** Место: `host/remotevoice/host_app.py` (`HostLease` захватывается до TLS, сопряжения и recovery). Тесты: `tests/test_host_lease.py` (`test_second_constructor_cannot_enter_initializer_or_recover_routes`, `test_no_lease_means_no_recovery_mutations`). Исходная находка: HOST-01.

Открытие второго окна вызывает recovery до блокировки владельца. В безопасной пробе все роли живого первого Host были переключены в физические устройства второго.

**Исправление:** Захватывать эксклюзивную блокировку всего приложения до восстановления и любых записей устройств; удерживать её до завершения работников и восстановления.

**Приёмка:** При активном первом Host второй экземпляр выполняет ноль записей маршрутов/mute/mixer; аварийный владелец допускает recovery только после освобождения lease.

Доказательства: [.harness/evidence/audit/agents/audit-host-repro-results.json](.harness/evidence/audit/agents/audit-host-repro-results.json).

### A02 — Остановка теряет ссылки на ещё живые аудиопотоки

**P1 · Воспроизведено · исправлено в коде, сверено 2026-11-08.** Место: `host/remotevoice/audio.py` (`stop` сохраняет живые потоки и бросает `AudioStopError`). Тест: `tests/test_audio.py::test_incomplete_stop_retains_workers_and_repeated_stop_confirms_exit`. Исходная находка: HOST-03.

После join(timeout=1) список работников очищается без is_alive. Инертный поток продолжал работать после возврата stop; сервер может начать новый движок на тех же endpoints.

**Исправление:** Сохранять ссылки, проверять завершение всех работников и сообщать cleanup failure; запрещать новый сеанс при незавершённой остановке. Изоляцию нативного движка в subprocess рассматривать отдельным шагом.

**Приёмка:** Зависший искусственный работник остаётся в учёте, новый AudioBridge не запускается; после освобождения работника подтверждается окончательная очистка.

Доказательства: [.harness/evidence/audit/agents/audit-host-repro-results.json](.harness/evidence/audit/agents/audit-host-repro-results.json).

### A03 — Отказ busy потребляет PIN и заменяет токен без передачи клиенту

**P1 · Воспроизведено · исправлено в коде, сверено 2026-11-08.** Место: `host/remotevoice/server.py` (слот занимается до чтения credential, `authenticate` только после подготовки маршрутов). Тест: `tests/test_server.py::test_busy_slot_does_not_read_or_consume_pin`. Исходная находка: PROTO-01.

При занятом слоте authenticate выполняется раньше проверки lock на строке 164. Новый PIN заменяет сохранённый токен, затем клиент закрывается с кодом 4002 до auth_ok. Старый токен и PIN уже недействительны, новый токен клиент не получил.

**Исправление:** Резервировать слот до commit сопряжения; сделать проверку ёмкости и смену токена одной атомарной операцией.

**Приёмка:** Busy-клиент не меняет токен/доступность PIN; свободный слот выдаёт новый токен ровно один раз и передаёт auth_ok.

Доказательства: [.harness/evidence/audit/protocol-repros.json](.harness/evidence/audit/protocol-repros.json).

### A04 — Проверка IPv4 пропускает внешний DNS-адрес

**P1 · Путь подтверждён кодом · открыто.** Место: `ios/RemoteVoice/PairingInfo.swift:46`. Исходная находка: CL-01.

compactMap удаляет нечисловые части. Значение 10.0.0.1.attacker.com превращается в четыре приватных октета для проверки, но исходный hostname используется URLSession. Pin из того же QR не ограничивает адрес локальной сетью. Реальная iOS-передача не выполнялась.

**Исправление:** Принимать ровно четыре ASCII-десятичных компонента без отбрасывания частей; общие отрицательные fixtures для Swift/Java/Python.

**Приёмка:** DNS-суффиксы, лишние/пустые октеты, Unicode и знаки отклоняются; допустимые приватные IPv4 принимаются на всех клиентах.

Доказательства: [.harness/evidence/audit/agents/audit-clients.md](.harness/evidence/audit/agents/audit-clients.md).

### A05 — Микрофон отправляет кадры без ограниченной очереди и ожидания ACK

**P1 · Путь подтверждён кодом · открыто.** Место: `ios/RemoteVoice/BridgeClient.swift:62`. Исходная находка: CL-02.

Запись начинается до mic_state, кадры помещаются в DispatchQueue/async socket.send без лимита возраста/числа. Mute может идти после старого голоса в том же сокете; UI считает изменение завершённым до ACK. Медленная сеть на iPhone не воспроизводилась.

**Исправление:** Контролируемый writer с лимитами и deadline; захват после matching ACK; mute немедленно останавливает захват, недоставляемый mute завершает сеанс.

**Приёмка:** Синтетический медленный socket не накапливает неограниченную очередь; stale voice не проигрывается после принятого mute; ACK timeout корректно закрывает сеанс.

Доказательства: [.harness/evidence/audit/agents/audit-clients.md](.harness/evidence/audit/agents/audit-clients.md).

### A06 — Ошибка включения микрофона не откатывает аудиодвижок

**P1 · Путь подтверждён кодом · открыто.** Место: `ios/RemoteVoice/PhoneAudioEngine.swift:34`. Исходная находка: CL-03.

Playback остановлен до throwing конфигурации; tap устанавливается до engine.start. Ошибка оставляет tap/остановленное воспроизведение, внешний catch лишь показывает false/error. Повторный enable может повторно установить tap.

**Исправление:** Сделать start транзакцией с unwind каждого шага; восстановить playback или явно завершить сеанс при неудачном восстановлении.

**Приёмка:** Fault injection в category/voice processing/converter/engine.start, затем повторный enable и disconnect не оставляют tap и не теряют воспроизведение молча.

Доказательства: [.harness/evidence/audit/agents/audit-clients.md](.harness/evidence/audit/agents/audit-clients.md).

### A07 — Поздние Task и permission callbacks меняют новый сеанс

**P1 · Путь подтверждён кодом · открыто.** Место: `ios/RemoteVoice/BridgeModel.swift:41`. Исходная находка: CL-04.

Callbacks и Task не проверяют identity текущего client. После await разрешения старый клиент способен перенастроить глобальную AVAudioSession, а его completion — состояние переподключённого клиента.

**Исправление:** Session UUID/generation, отмена задач, проверки до/после await и один сериализованный владелец состояния клиента.

**Приёмка:** Отложить permission/receive/send A, закрыть A и начать B; поздние результаты A не меняют B, его mic или AVAudioSession.

Доказательства: [.harness/evidence/audit/agents/audit-clients.md](.harness/evidence/audit/agents/audit-clients.md).

### A08 — Ошибка вывода оставляет частично активированный сеанс

**P2 · Воспроизведено · исправлено в коде, сверено 2026-11-08.** Место: `host/remotevoice/host_app.py` (результат `_activate_output_route` проверяется, при отказе `_stop_host` с сохранением первичной ошибки). Тесты: `tests/test_host_lease.py::test_partial_activation_restores_all_and_preserves_primary_error`, `tests/test_host_output_routing.py`. Исходная находка: HOST-02.

Физический mic заглушён, виртуальный вход выбран; _activate_output_route возвращает false, но результат игнорируется. Проба оставила server_running=true и ноль вызовов остановки.

**Исправление:** Проверять результат и откатывать все применённые шаги; сохранить первичную ошибку и отказать в активации сеанса.

**Приёмка:** Отказ любого шага mute/input/output приводит к независимому восстановлению остальных и исключает частично активное состояние.

Доказательства: [.harness/evidence/audit/agents/audit-host-repro-results.json](.harness/evidence/audit/agents/audit-host-repro-results.json).

### A09 — Поздний ответ экрана принимается за ответ нового запроса

**P2 · Воспроизведено · исправлено в коде, сверено 2026-11-08.** Место: `BridgeConnection.java` (`request_id` в screen_request и точная сверка с `screenRequestEpoch`). Тесты: `tests/test_screen_server.py` (эхо request_id на Host), `android/app/src/test/.../BridgeConnectionTest.java` (Gradle 2026-11-08 не запускался). Исходная находка: CL-06.

После timeout запроса 101 разрешён 102; поздний screen_offer 101 передаётся listener как 102. Java probe воспроизвёл stale ticket accepted=true. Host при новой выдаче заменяет ticket, поэтому просмотр получает отказ.

**Исправление:** Одинаковый строковый request ID в screen_request/offer/error и точное matching; старые ответы отбрасывать после timeout/cancel.

**Приёмка:** Поздние offer/error 101 после запуска 102 не меняют 102; отмена, новая попытка и revoke сохраняют защиту ticket.

Доказательства: [.harness/evidence/audit/agents/audit-clients-probe-result.txt](.harness/evidence/audit/agents/audit-clients-probe-result.txt).

### A10 — Unicode credential вызывает необработанный TypeError

**P2 · Воспроизведено · закрыто 2026-11-08.** Место: `host/remotevoice/pairing.py` (`_valid_credential`), `server.py` (отказ учитывается в `PairingAttemptLimiter`). Приёмка: `tests/test_pairing.py::test_malformed_credentials_cannot_change_pairing`. Исходная находка: PROTO-02.

hmac.compare_digest двух str не поддерживает не-ASCII. Проба credential=тест получила TypeError вместо нормального отказа; auth handler не перехватывает этот тип и не отмечает такой отказ в limiter.

**Исправление:** Проверять разрешённый ASCII-формат credential до сравнения; malformed auth обрабатывать как контролируемый отказ с учётом лимитера.

**Приёмка:** Unicode, чрезмерная длина и неверные типы дают отказ без traceback и обхода учёта попыток.

Доказательства: [.harness/evidence/audit/protocol-repros.json](.harness/evidence/audit/protocol-repros.json).

### A11 — NaN проходит протокол и достигает аудиоадаптера

**P2 · Воспроизведено · закрыто 2026-11-08.** Место: `host/remotevoice/protocol.py` (`validate_audio_payload` отклоняет NaN/Inf в обоих направлениях: `submit_phone_audio` и `encode_system_frame`). Приёмка: `tests/test_audio.py::test_nonfinite_microphone_frames_never_reach_queue_or_player`. Исходная находка: PROTO-03.

decode_audio_frame проверяет размер, но не числа. Синтетический кадр из 480 NaN прошёл submit_phone_audio и был передан fake player.play. Реальное устройство и опасный сигнал не использовались.

**Исправление:** Отклонять NaN/Inf до аудиоадаптера; явно определить обработку значений вне нормализованного диапазона и проверить оба направления.

**Приёмка:** NaN/Inf никогда не достигают player; корректные кадры и допустимая амплитуда сохраняются; ошибки не раскрывают содержимое аудио.

Доказательства: [.harness/evidence/audit/protocol-repros.json](.harness/evidence/audit/protocol-repros.json).

### A12 — Ошибка соединения не инвалидирует URLSession

**P2 · Путь подтверждён кодом · открыто.** Место: `ios/RemoteVoice/BridgeClient.swift:147`. Исходная находка: CL-05.

connectionFailed отменяет socket, но не вызывает invalidateAndCancel и не очищает session/socket; URLSession удерживает delegate=self, self удерживает session. Профилирование iOS не выполнялось.

**Исправление:** Один идемпотентный terminal cleanup для обычного закрытия и всех ошибок; инвалидирование session, освобождение ссылок и завершение задач.

**Приёмка:** Серия failure/reconnect освобождает старые клиенты по weak-проверке и оставляет ровно одну активную session.

Доказательства: [.harness/evidence/audit/agents/audit-clients.md](.harness/evidence/audit/agents/audit-clients.md).

### A13 — Чистые CI-сборки не сохраняют подпись обновления APK

**P2 · Наблюдаемая конфигурация · исправлено в коде, сверено 2026-11-08; требует настройки владельцем.** Место: `android/app/build.gradle` (подпись из `REMOTEVOICE_RELEASE_*` с проверкой сертификата), `.github/workflows/android.yml` (PR и форки собирают только unsigned; job `signed-release-candidate` включается переменной `REMOTEVOICE_ANDROID_RELEASE_ENABLED` и environment `android-release` с секретами ключа). Исходная находка: D03.

Hosted runner собирает debug APK без provisioning стабильного ключа; build.gradle не задаёт проектную signing identity. Новая чистая сборка может не обновиться поверх установленного локального APK. Текущая установленная 0.1.3 этим не объявляется сломанной.

**Исправление:** Стабильная разрешённая владельцем подпись для тестовых обновлений; защищённое хранение ключа либо локальная подпись; сертификат в artifact manifest.

**Приёмка:** Две чистые сборки имеют одинаковый публичный signer fingerprint и обновляют приложение без удаления данных; приватного ключа в Git нет.

Доказательства: [.harness/evidence/audit/agents/audit-delivery.md](.harness/evidence/audit/agents/audit-delivery.md).

### A14 — Fingerprint не включает конфигурации, которые читают тесты

**P2 · Наблюдаемая конфигурация · открыто.** Место: `.harness/state.json`. Исходная находка: D01.

CHK-022 содержит Python-тесты конфигурации, но не iOS Info.plist и AndroidManifest.xml, которые они читают. Исторические native checks наследуют этот набор. validate проверяет объявленные зависимости и не обнаруживает пропуск.

**Исправление:** Полный переиспользуемый manifest входов и отдельная identity runtime; затронутые проверки пересчитать только после реального повторного запуска.

**Приёмка:** Изменение каждой прочитанной конфигурации меняет fingerprint; текущий прогон использует полный набор, старые данные сохраняются как история.

Доказательства: [.harness/evidence/audit/agents/audit-delivery.md](.harness/evidence/audit/agents/audit-delivery.md).

### A15 — Рабочий Host запускается диагностическим wrapper без build manifest

**P2 · Наблюдаемая конфигурация · исправлено в коде, сверено 2026-11-08.** Место: `host/remotevoice/deployment.py` (`write_receipt` пишет `runtime.json` с PID, версией и SHA-256 exe или снимка исходников сразу после захвата lease), `host/launcher.py` — единственная точка входа. Тесты: `tests/test_deployment.py`. Исходная находка: D02.

Wrapper подменяет _set_status и автоматически запускает Host; обычный launcher отличается. Текущий процесс/порт прочитаны, но startup receipt не связывает загруженный Python snapshot с artifact hash. Хеш текущего диска не доказывает загруженные ранее байты.

**Исправление:** Один production launcher с опциональной диагностикой, startup receipt PID/start/build/root/dependencies и immutable deployment evidence.

**Приёмка:** Проверка разрыва относится к идентифицированной нормальной сборке; изменения диска не считаются применёнными до перезапуска.

Доказательства: [.harness/evidence/audit/runtime-identity.json](.harness/evidence/audit/runtime-identity.json).

### A16 — Текущее локальное дерево не имеет Git baseline

**P2 · Наблюдаемая конфигурация · закрыто 2026-11-08.** Место: `.git` (первый коммит `96a2381`, 355 файлов; `.harness` намеренно вне Git по `.gitignore`). Исходная находка: D04.

git ls-files дал0, коммитов нет, все исходники untracked. Исторический GitHub CI не связывает текущие исправления M: с проверенным commit. Пустой git diff не означает отсутствие изменений.

**Исправление:** Сначала сверить репозиторий/историю, затем сохранить проверенный source snapshot/commit по явному allowlist; исключить account/setup evidence и ключи.

**Приёмка:** Текущие исходники и артефакты имеют восстанавливаемую identity; чистый checkout воспроизводит проверки; случайные архивы и секреты не опубликованы.

Доказательства: [.harness/evidence/audit/agents/audit-delivery.md](.harness/evidence/audit/agents/audit-delivery.md).

### A17 — Активные разрешения и решения противоречат новым фактам

**P2 · Наблюдаемая конфигурация · открыто.** Место: `.harness/state.json`. Исходная находка: D05.

PERM-002/DEC-004 и часть blockers всё ещё описывают отсутствие авторизации/аудиопар рядом с PERM-012 и реальными установленными каналами; старые пути AltServer/iloader/account остаются активными. README и root SPEC расходятся с текущими срезами. Новая архитектура объединяет карту требований, но ledger/документы ещё требуют согласования.

**Исправление:** Один актуальный ledger решений с superseded/history, отдельные срезы Audio/Screen/Transport/LocalSign и карта всех25требований.

**Приёмка:** Для текущего этапа нет противоречащих setup/permission/blocker; прогресс Android не закрывает iPhone/LocalSign.

Доказательства: [.harness/evidence/audit/agents/audit-delivery.md](.harness/evidence/audit/agents/audit-delivery.md).

### A18 — Проверка hash не связывает байты, которые будут подписаны

**P2 · Путь подтверждён кодом · исправлено в коде, сверено 2026-11-08.** Место: `signer/app/src-tauri/src/main.rs`, `ipa.rs` (подпись идёт из защищённого snapshot, handle удерживается до конца операции). Rust-тесты: `cargo test --locked` — 20 passed (2026-11-08). Исходная находка: D06.

После проверки hash путь вновь используется после await и открывается библиотекой. Замена файла в этом интервале позволяет подписывать другие байты, чем показанные пользователю. Реальная подмена/подпись не выполнялась.

**Исправление:** Из открытого handle создать приватный неизменяемый snapshot; анализ, hash и подпись используют один snapshot.

**Приёмка:** Замена исходного пути после подтверждения не меняет реально переданные на подпись байты; их hash совпадает с одобренным.

Доказательства: [.harness/evidence/audit/agents/audit-delivery.md](.harness/evidence/audit/agents/audit-delivery.md).

### A19 — Нет общей политики дедлайнов и восстановления неизвестной установки

**P2 · Путь подтверждён кодом · исправлено в коде, сверено 2026-11-08.** Место: `signer/app/src-tauri/src/operation.rs` (журнал `install-effects`, `InstallEffect::begin`, `pending_install`, `resolve_install_effect`; таймаут оставляет durable gate вместо слепого повтора). Rust-тесты: `cargo test --locked` — 20 passed (2026-11-08). Исходная находка: D07.

busy lock удерживается через device/install await без общей политики отмены/дедлайна. Нет durable effect ID и восстановления unknown после перезапуска. Фактическое зависание внешней библиотеки не воспроизводилось.

**Исправление:** InstallOperationCoordinator с immutable IPA hash, target/effect ID, фазами, bounded discovery и reconciliation неизвестного эффекта до повтора.

**Приёмка:** Зависшие synthetic futures не блокируют UI навсегда; перезапуск восстанавливает pending operation; таймаут не запускает слепой повтор.

Доказательства: [.harness/evidence/audit/agents/audit-delivery.md](.harness/evidence/audit/agents/audit-delivery.md).

## Выполненные проверки

- 183 существующих Host-теста повторно выполнены успешно; compileall прошёл. В тестовом runner изменяется только фабрика временных папок для наследуемых ACL, нативные устройства не открываются.
- 43 JVM-теста и Android lint (0 ошибок, 8 предупреждений) принудительно повторены с `--rerun-tasks`; итоговый лог и XML сохранены в evidence. JVM-проверки не заменяют Android media/FGS/UI и настоящий BitmapFactory.
- Три Host-пробы, три протокольные пробы и одна Java-проба preview воспроизвели описанные ошибки. Подставной поток освобождён и завершён. Swift сценарии проверены по коду; Swift/iPhone runtime не выполнялся.
- Только чтением установлены текущие Host PID/entry point/владелец порта и Android versionCode 4 / versionName 0.1.3. PID 62884 слушает 10.20.30.2:8765; launcher 52736. Это наблюдение процесса, не доказательство fingerprint загруженного кода.
- Попытка `cargo test --locked --offline` завершилась до тестов: build script num-traits/autocfg сообщил `output path is not a writable directory`. Этот сбой сборочной среды не выдан за падение тестов LocalSign или за доказанную ошибку его исходников. Старые Rust-результаты не повышены до текущих.

## Границы приёмки

Ранее пользователь подтвердил возврат динамиков/микрофона после отключения и исчезновение цифрового эха после разделения AUX/B1. Эти факты сохранены. Аудит не воспроизводил отказы на настоящих аудиоустройствах, не перезапускал действующий Host и не запускал новую запись/захват экрана.

Остаются непроверенными: долгая речь/фон/audio focus, собственно изображение preview и его lifecycle, акустическое эхо динамика, исходно заглушённый mic и реальная авария Host. USB/hotspot/Ethernet требуют физической проверки; Bluetooth требует реализации codec/transport. Подпись/установка на iPhone, собственный Anisette, автопродление и Store не завершены.

Дополнительные проектные пробелы, не включённые в 19 ошибок: у Tk-событий нет session generation; адаптер Voicemeeter не запрашивает тип запущенного продукта перед индексами Banana; Android AudioRecord/AudioTrack/FGS/Bitmap не покрыты JVM suite; у PIN нет таймерного expiry; отсутствуют общий protocol version/capabilities и текущая чистая упаковка всех продуктов.

## Единая карта требований

| Требование | Контур | Состояние приёмки |
| --- | --- | --- |
| REQ-001 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-002 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-003 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-004 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-005 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-006 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-007 | Windows + iPhone audio/install | iPhone не принят; Android/native наблюдения не закрывают исходную iPhone-приёмку |
| REQ-008 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-009 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-010 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-011 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-012 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-013 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-014 | LocalSign | Прототип/исходники; реальная подпись, Anisette, renewal и Store открыты |
| REQ-015 | Android audio/install | APK установлен; частичные реальные сценарии; длительная речь и фон открыты |
| REQ-016 | Android audio/install | APK установлен; частичные реальные сценарии; длительная речь и фон открыты |
| REQ-017 | Android audio/install | APK установлен; частичные реальные сценарии; длительная речь и фон открыты |
| REQ-018 | Android audio/install | APK установлен; частичные реальные сценарии; длительная речь и фон открыты |
| REQ-019 | Windows recovery | Реальный возврат подтверждён; A01/A02/A03/A08 и аварийная приёмка открыты |
| REQ-020 | Windows recovery | Реальный возврат подтверждён; A01/A02/A03/A08 и аварийная приёмка открыты |
| REQ-021 | Screen | Исходники/APK и синтетические проверки; A09 и настоящее изображение открыты |
| REQ-022 | Windows recovery | Реальный возврат подтверждён; A01/A02/A03/A08 и аварийная приёмка открыты |
| REQ-023 | Transport | Исследование/архитектура; дополнительные режимы не приняты |
| REQ-024 | Transport | Исследование/архитектура; дополнительные режимы не приняты |
| REQ-025 | VM routing | Цифровая петля исправлена и подтверждена; акустическое эхо проверяется отдельно |

Состояние всего проекта — implementation / in_progress. Аудит завершает только TASK-024; он не закрывает продуктовые требования и не объявляет релиз готовым.
