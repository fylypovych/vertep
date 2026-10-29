# Review контрактів Architecture 2.0 — #367

Дата: 2026-10-29. Статус: review завершено, пакет погоджено власником у чаті. Це матеріал приймання #367, не окремий backlog.

## Межі та версія

Джерела: [#108](https://github.com/fylypovych/vertep/issues/108), [#367](https://github.com/fylypovych/vertep/issues/367), [#368](https://github.com/fylypovych/vertep/issues/368), [#369](https://github.com/fylypovych/vertep/issues/369). Перевірено body/comments ланцюжка #108 → #272 → #298 → #323 → #346 → #367: явного погодження контрактів і старту implementation не знайдено. Коментарі про завершення аудиту/перенесення прямо відокремлюють це від approval.

Review виконано на HEAD `a98e2de4a19a6906c9403e8495a37275baa8f445` (VERSION `0.0.2.87`). Остання зміна шести документів: commit `34146649a923ed28a638554fa6593495dcebac96` (`0.0.2.77`). Канонічні документи не переписано. Git blob ідентифікує точні байти документа незалежно від Windows line endings.

| Документ у `contracts/` | Git blob |
| --- | --- |
| `workflow_dag_contract.md` | `576131ebe93f5db98694c259b5681f0085c3bc14` |
| `module_contract.md` | `d1ff1f1c59d98d551791e2828e7273c9423d8dd5` |
| `core_worker_protocol_contract.md` | `d3b46e6506e1ca08f1f269d5cdc4ab5fcb736595` |
| `artifact_contract.md` | `3666335406e3426a1eb59b5e21cc5da51b7c9ce8` |
| `gpu_provider_contract.md` | `a02a04109d777127199a04a538169100bd682372` |
| `backup_restore_contract.md` | `acf261712399907f5534aabf2d60d7017793eea7` |

## Результат review і запропоновані уточнення

Уточнення нижче конкретизують уже погоджені вимоги #108 та acceptance #367–369. Вони не додають нових екранів, providers або продуктових сценаріїв. Approval цього пакета означатиме погодження шести документів разом із цими уточненнями, а не підтвердження готовності реалізації.

### Workflow DAG

Поля task input/output/dependencies/timeout/retry та стани job/task/approval описані. YAML є фрагментом `tasks`, хоча кореневі `name/version/description` оголошено обов'язковими. Не визначено точну семантику retry, timeout, pause/cancel та взаємодію revision з approval.

Пропозиція: повний документ має включати кореневі поля; перевіряти весь DAG на дублікати id, відсутні dependencies і цикли, без вимоги порядку оголошення tasks. `max_attempts` включає першу спробу; timeout task застосовується до однієї спроби. Pause зупиняє новий dispatch, поточна спроба завершується; cancel припиняє dispatch і робить пізній результат скасованої спроби неприйнятним. Revision створює нову версію відповідного output та потребує нового approval перед залежним виконанням; збережене approval старого output не переноситься автоматично. Conditional branches мають явні умови в definition, без виконання довільного Python. Конкретна DSL-форма перевіряється в #368 у межах вимог #108.

### Module

Manifest містить name/version/dependencies/capabilities/resources та lifecycle/API. Приклад використовує вкладений `manifest`, SemVer `1.0.0` і dependencies з діапазоном версій. Прототип `core/module/manifest.py` читає flat-поля, приймає лише чотирикомпонентну версію й dependencies як рядки. Це розбіжність контракту й реалізації. Підпис зовнішніх модулів із #368 AC2 у документі не конкретизовано.

Пропозиція: версію module і compatibility трактувати окремо від чотирикомпонентної версії Vertep; підтримати документований manifest і діапазони dependencies, зберегти читання наявного формату через явну сумісність. Перевіряти підпис manifest та вмісту зовнішнього пакета довіреним налаштованим ключем до завантаження коду; відсутній/недійсний/недовірений підпис відхиляє установлення. Не вбудовувати secrets чи універсальний довірений ключ у код. Unload/uninstall не може позбавляти активну task її виконавця: спочатку завершення або кероване cancel. Discovery рекламує лише завантажені сумісні модулі з фактично перевіреними capabilities/resources.

### CORE ↔ Worker

Assignment, registration, heartbeat, lease та lifecycle описані. JSON-приклад містить `{...}` і `true|false`, тому є схемним псевдокодом, а не валідним JSON. ProtocolVersion заявлено, але немає поля версії в envelope. Progress/result/cancel не містять ідентичності власника та конкретної lease; не визначено гонки renewal/revocation/late result.

Пропозиція: CORE є авторитетом призначення; кожна спроба має worker identity та окрему lease identity/generation. Claim атомарний. Progress/result/renew/cancel перевіряються за поточною спробою та lease; після expiry/revoke/reassignment старі повідомлення не змінюють актуальний стан. Строки lease передаються як UTC timestamps; renewal не воскресає після revoke. Envelope явно несе protocol version; compatibility перевіряється при registration до dispatch. Документований SemVer протоколу незалежний від VERSION Vertep; старі формати підтримуються адаптером у межах заявлених двох release cycles, а не мовчазним прийманням несумісних повідомлень. Приклади мають розділяти schema та виконуваний JSON із конкретними значеннями.

### Artifact

Metadata, checksum, task/job references, transfer token, storage/cache/TTL описані. `abc123...` є placeholder, а `owner: worker_id|core` описує custody, не project/ACL із #108. Версію metadata, строк transfer token та правила видалення залежних artifacts не визначено.

Пропозиція: явно відокремити schema version, executor custody та owner/project/ACL metadata; останнє є підготовкою схеми, без впровадження повної multiuser-системи. Transfer token має обмежений строк і одного актуального claimant; retry/expiry не дозволяє подвійне приймання. Приймання потребує повного SHA256 та розміру; для resumable upload підсумкова перевірка охоплює весь файл. TTL cleanup не видаляє artifact, необхідний незавершеній task, approval або checkpoint. Валідний приклад мусить містити 64 hex символи SHA256 фактичного прикладу даних.

### GPU provider

Provider API, priority, readiness/resources, fallback та lease описані. Не розрізнено логічне звільнення lease й фактичне завершення inference; force-release CORE сам по собі не доводить звільнення VRAM. Не уточнено невизначений результат submit.

Пропозиція: вибір і fallback лише серед configured allowlist, із дотриманням priority, readiness, capacity та local-only. Вичерпання кандидатів повертає явну помилку з причинами. До submit резервувати capacity; після невизначеного submit не запускати дубль без з'ясування статусу/керованого припинення попередньої спроби. Revoke/timeout блокує приймання старого результату, але capacity повертається лише після підтвердження завершення/скасування або перевіреного відновлення provider. Набір типів provider залишається в погодженому scope #368; нові integrations не додаються.

### Backup / restore

Full/incremental snapshots, checksum, encryption, selective restore/resume та module state описані. `version: 0.0.2.76` змішує release та формат snapshot; `{...}` у JSON і скорочений checksum є псевдокодом. Не визначено узгоджений зріз jobs/tasks/approvals/artifacts і поведінку живих lease після restore.

Пропозиція: окремі schema version і source release version; snapshot містить узгоджений зріз стану та посилання на потрібні module versions/artifacts. Incremental restore перевіряє весь ланцюжок base і checksums до застосування; відсутня база/ключ/артефакт не оголошується успішним restore. Зберігати reference до ключа, не секрет у snapshot metadata. Після restore не відновлювати старі execution leases як активні; recovery узгоджує спроби й fence до resume, зберігаючи approval саме відповідної revision. Selective restore відхиляє неповний dependency closure або явно повідомляє degraded стан без запуску залежних tasks.

## Перевірки та залежності

Focused baseline: `tests/test_architecture_20_integration.py`, `tests/test_workflow_engine_checkpoint.py`, `tests/test_module_registry.py` — **27 passed**. Локальний звіт: `.pytest_tmp/issue-367-review-baseline.xml`. Це перевірка наявного прототипу, не доказ actual DAG dispatch, external signature, provider execution чи recovery.

Збережені виправлення імпорту Dispatcher, reload `_jobs` і persistence task dependencies/type повторно не реалізовуються. Поточний `WorkflowEngine` усе ще викликає `_complete_task_simulation`; це scope #368 після owner gate.

- #367 завершується після review та явного рішення власника щодо цього пакета й старту implementation. Код #368/#369 і production qualification не є критерієм завершення #367.
- #368 реалізує DAG/manifest/protocol/GPU/Telegram integration після approval #367.
- #369 відповідає за module migration, фактичні execution outputs та checkpoint backup/restore/process-restart/resume, із залежностями інтеграції #368.
- #326 зберігає physical migration та conditional retirement. Його real-server acceptance не замінюється unit tests і не блокує автоматизоване виконання #368.

## Рішення власника

**APPROVED — 2026-10-29.** Власник у відповідь на запит погодження шести контрактів разом з уточненнями цього review та старту #368 прямо відповів: «Погоджую контракти з уточненнями та початок #368». Рішення отримано в поточному чаті, не в GitHub. Воно закриває owner gate #367; не є твердженням про готовність #368/#369 або production qualification. Дозвіл на push/release до цього рішення не входить.
