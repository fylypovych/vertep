# Changelog

## Unreleased

## ПРАВИЛЬНА НАЗВА: 0.0.1.50
- Виправлено `test_health_check_structure` у `tests/test_bootstrap_wizard.py`: додано `CONFIG_ROOT` у `tmp_path` для уникнення `PermissionError` на CI.
- Виправлено `GitHubActionsTrigger.run` у `scripts/release.py`: `workflow_dispatch` використовує `main` замість commit SHA.

## ПРАВИЛЬНА НАЗВА: 0.0.1.49
- Перероблено Telegram transport: polling (`getUpdates`) став типовим, webhook — legacy; додано exponential backoff, persistent offset з `os.fsync`, `last_error` та `consecutive_failures`.
- Видалено `core/api/telegram.py`; логіка polling перенесена до `adapters/telegram.py`.
- Оновлено `TelegramSetup` у `core/models.py`: `public_url` позначено як legacy.
- Додано `last_error` та `consecutive_failures` у `/api/telegram/status`.
- Видалено `web-v2/src/app/queue/queue.component.ts` та роут `/queue`; черга перенесена у вкладку «Завдання».
- Рефакторинг `settings.component.ts` на секції з новим каталогом `web-v2/src/app/settings/sections/`.
- Додано спільні UI-компоненти стану (`LoadingStateComponent`, `ErrorStateComponent`, `EmptyStateComponent`).
- Оновлено dashboard: null-safe метрики з fallback «Немає даних».
- Перейменовано в бічній панелі: «Операції» → «Алерти».
- Додано 4 Playwright E2E тести для First Run Wizard та unit-тести для Telegram polling.
- Додано `test_bootstrap_wizard.py` для bootstrap wizard.
- Оновлено документацію `README.md`, `agents/README.md`, `telegram/README.md`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.48
- Додано Telegram approval flow для сценаріїв: callback-хендлери `sc_ok`, `sc_regen`, `sc_edit`, `sc_reject` у `core/app.py`, функції `render_script()`, `script_keyboard()`, `_send_script_approval_to_telegram()` у `core/storyboard_telegram.py` та `core/pipeline.py`.
- Додано Telegram approval flow для відео: callback-хендлери `vid_ok`, `vid_regen`, `vid_edit`, `vid_reject` у `core/app.py`, функції `video_approval_keyboard()`, `send_video_for_approval()`, `approve_video()`, `request_video_revision()`, `regenerate_video()` у `core/pipeline.py` та `core/storyboard_telegram.py`.
- Розширено `JOB_STATE_TRANSITIONS`: додано `VIDEO_READY` до можливих переходів з `VIDEO_APPROVED` у `core/models.py`.
- Оновлено `_prepare_and_dispatch()`: обробка статусів `VIDEO_PENDING_APPROVAL` та `VIDEO_REVISION_REQUESTED` у `core/api/job_helpers.py`.
- Додано тести Telegram approval flow: `tests/test_telegram_approval_flow.py`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.47
- Виправлено нумерацію версій: commit `7d57408a` мав назву "0.0.1.39" замість "0.0.1.46". Оновлено `VERSION`, `CHANGELOG.md` та release notes.

## ПРАВИЛЬНА НАЗВА: 0.0.1.46
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.39
- Виправлено `handleError` в `api.service.ts`: коректно витягує `detail` з `HttpErrorResponse.error.detail` та зберігає HTTP `status` для подальшої обробки 409 conflicts.
- Додано обробку 409 conflict у `runAction` компонента `job-detail`: при 409 встановлюється `conflict` signal для відображення блоку «Конфлікт версії».
- Додано українські мітки `revision` (Правки) та `reject` (Відхилення) до `actionLabel` в `job-detail.component.ts`.
- Оновлено browser E2E тест `test_script_backend_error_shows_error_and_no_crash`: перевіряється реальний текст помилки з бекенду замість загального «Помилка».
- Додано конфігурацію production build в `angular.json` (`outputHashing`, `optimization`, `extractLicenses`).
- Додано тести для role services: CRUD, recreate, backup→wipe→restore для персонажів/брендів/workflow/jobs.

## ПРАВИЛЬНА НАЗВА: 0.0.1.45
- Видалено тимчасові файли: `error-context.md`, `playwright-report/`, `test-results/.last-run.json`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.44
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.36
- Порожній commit.

## ПРАВИЛЬНА НАЗВА: 0.0.1.43
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.36
- Оновлено `AGENTS.md`: розширено правила версіонування, роботи з Issues та Provider/Adapter Layer.

## ПРАВИЛЬНА НАЗВА: 0.0.1.42
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.42
- Розширено `core/repository.py`: додано операції persistent user data (backfill, migrate, backup, restore).
- Оновлено `db/010_persistent_user_data_backfill.backfill.py`: реалізовано backfill та міграцію persistent user data з deduplication.
- Додано тести `tests/test_backfill.py` та `tests/test_repository.py`.
- Замінено `web-v2/tests/admin.spec.ts` на `web-v2/tests/brands-crud.spec.ts`: CRUD-тест брендів через Playwright.
- Оновлено `scripts/migrate.py` та `scripts/vertep`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.41
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.35
- Відновлено `AGENTS.md` після випадкового видалення (0.0.1.39); видалено `AGENTS.md.tmp`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.40
## ОРИГІНАЛЬНА НАЗВА: tmp
- Тимчасовий commit: створено `AGENTS.md.tmp`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.39
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.34
- Випадково очищено `AGENTS.md` до одного рядка (відновлено в 0.0.1.41).

## ПРАВИЛЬНА НАЗВА: 0.0.1.38
- Додано regression-тест для Telegram creation feedback: помилки вибору персонажа та збереження Job показуються користувачу, а невдалий запит можна повторити.
- Додано browser tests для відображення API-помилки створення Job і автоматичного оновлення списку Job після Telegram creation.
- Задокументовано історичні release notes `0.0.1.35` і `0.0.1.36`; поточний release включає зміни storyboard approval, Backup/Restore та міграції channels.

## ПРАВИЛЬНА НАЗВА: 0.0.1.37
- Виправлено `UnboundLocalError` у `core/persistent_data.py` при повторному запуску `ensure_persistent_user_data()`.
- Розширено storyboard: додано `image_storyboard_task_versions` для відстеження версій завдань, `image_prompt_history` для історії промптів та `send_storyboard_images` для відправки превʼю в Telegram.
- Оновлено `services/backup_service.py`: додано дефолтні команди `pg_dump`/`redis-cli BGSAVE`, змінено логіку restore на роботу з дампами, додано post-restore health check та переведення в EMERGENCY при помилці.
- Додано `BACKUP_PG_RESTORE_CMD` у `deploy/docker-compose.yml`, розширено `migrate` service змінними середовища та volumes.
- Оновлено `.gitignore` та `AGENTS.md`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.36
- Розширено `services/backup_service.py`: retention за віком і кількістю snapshot, налаштовувана команда remote copy та команди dump PostgreSQL/Redis, показ inventory і прогресу restore.
- Додано перевірку стану перед restore, облік помилок і перевірку доступності каталогів після відновлення; у CORE додано endpoints стану системи та прогресу restore.
- Розширено `scripts/restore.sh` підтримкою `.vtbackup` через Backup Service, перевіркою стану системи й доступності CORE після restore; додано параметри backup у `.env.example`.
- Додано тести retention, блокування restore в EMERGENCY, прогресу та додаткових джерел backup.
- До commit включено допоміжні файли `patch_settings.py`, `patch_settings2.py` та `settings.patch`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.35
## ОРИГІНАЛЬНА НАЗВА: 0.0.1.34
- Додано у Web UI показ складу backup, прогресу restore та стану системи; керування кнопками й опитування прогресу відновлення.
- Розширено API service методами читання стану системи та прогресу restore, а `BackupInfo` — полями inventory, retention і remote copy.
- Додано тестовий workflow `workflows/image/test_persist.json` із вузлом `LoadImage`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.34
- Дозакрито Issue #6: негативний тест блокування відео до `image_status == "approved"` та перевірка збереження старих артефактів при `regenerate`; розширено REST-контракт на `images/approve|revision|regenerate`.
- Доповнено Issue #32: вирівняно `docker-compose.yml` під prod (`JOB_ROOT=/data/storage/jobs`, видалено окремий volume `jobs`), додано інтеграційні тести CRUD→recreate→видалення→recreate та backup→wipe→restore для персонажів/брендів/workflow/jobs (295 passed).

## ПРАВИЛЬНА НАЗВА: 0.0.1.33
- Реалізовано Issue #16: завершено Backup/Restore та Backup Node — реальний backup-сервіс із AES-256-GCM шифруванням, retention policy, remote storage hooks, кастомними inventory джерелами, restore progress, system-state gating та post-restore health verification.
- Розширено `services/backup_service.py`: підтримка `BACKUP_SOURCES`, `BACKUP_RETENTION_DAYS`, `BACKUP_MAX_SNAPSHOTS`, `BACKUP_REMOTE_CMD`, `BACKUP_CORE_URL`, `BACKUP_PG_DUMP_CMD`, `BACKUP_REDIS_DUMP_CMD`; додано `/snapshots/{id}/restore/progress` та `/system/status`.
- Оновлено `core/app.py`: додано `/api/system/state` та проксі `/api/system/backups/{snapshot_id}/restore/progress`.
- Оновлено Web UI V2: `SettingsComponent` з inventory, прогрес restore, system-state попередженням, disable кнопок та polling прогресу; розширено `BackupInfo` та API service.
- Оновлено `scripts/restore.sh`: гейти за станом, `.vtbackup` підтримка, post-restore health check.
- Додано тести: retention, emergency block, restore progress, custom sources; усі 312 тестів проходять.

## ПРАВИЛЬНА НАЗВА: 0.0.1.32
- Реалізовано image storyboard для Issue #6: після approval сценарію генерується `StoryboardVersion` із `preview` зображеннями кожної сцени через GPU Worker/ComfyUI (`queue_image_storyboard`), зберігається зв'язок `scene → prompt → artifact → version` (`image_artifact_id`, `image_version`, `image_status`).
- Додано цикл затвердження image storyboard: `POST /api/jobs/{id}/storyboards/images/{approve|revision|regenerate}` з версіонуванням, підтримкою `scene_indexes` та `revision`; `StoryboardService.approve_images` / `request_image_revision` з `409` при застарілій версії.
- Заблоковано перехід до `VIDEO_GENERATION`/`ASSET_GENERATION` до `image_status == "approved"` — `StoryboardService.approve` вимагає попереднього схвалення прев'ю, `job_helpers._prepare_and_dispatch` ставить у чергу генерацію прев'ю та чекає затвердження.
- Розширено Web UI V2 (`job-detail`, `api.service`) та Telegram (`render_storyboard`, `storyboard_keyboard`, `sb_img_*` callbacks) для показу всіх scene previews, `approve` всього storyboard та `revision`/`regenerate` окремих/усіх сцен; версіонування зберігає старі артефакти (`storyboard/v<версія>`).
- Додано E2E сценарій `tests/test_image_storyboard_e2e.py`: `script approved → images generated → revision → regenerated images → storyboard approved → READY`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.31
- Виправлено `TS2488` у перегляді сценарію: типізовано й перевірено масив сцен; додано браузерні тести (пов’язано з Issue #20).
- Видалено старі файли планів після перенесення вимог у GitHub Issues; у `AGENTS.md` визначено правила роботи агентів з Issues.
- Оновлено README та документацію: аудити позначено історичними, застарілий план версіонування замінено посиланням на чинні правила.
- Узгоджено нумерацію з чинною послідовністю `0.0.1.30 → 0.0.1.31`; історичні commit і tags залишено без змін.

## ПРАВИЛЬНА НАЗВА: 0.0.1.30
- Синхронізовано життєвий цикл сценарію та розкадровки: `SCRIPT_*` статуси узгоджено з `presentation.ts` та `job-detail` (approve/revision для сценарію).

## ПРАВИЛЬНА НАЗВА: 0.0.1.27
- Додано модуль `presentation.ts`: централізовані мітки статусів (`statusLabel`), ролей (`roleLabel`), груп статусів (`inStatusGroup`), станів дій (`jobActionAllowed`).
- Збагачено контекст вузлів у API: `/api/nodes` та `/api/nodes/{id}` повертають `modules`, `services`, `capability_backends`.
- Додано показ офлайн-зареєстрованих вузлів у списку worker-ів зі статусом OFFLINE.
- Виправлено пошук вузла за `node_id` або `node_name` у `control_node`.
- Локалізовано Web UI V2: ролі вузлів, статуси джобів, статуси вузлів через централізовані мітки.
- Перейменовано розділ «Воркери» → «Вузли» у sidebar та workers.
- Додано фільтр за групами статусів (активні/в черзі/очікують/завершені/помилки) у список джобів.
- Розширено пошук джобів: пошук по темі (topic) крім ID.
- Зроблено карточки статистики «Активні завдання» та «Завдання в черзі» на Dashboard клікабельними посиланнями.
- Додано секцію «Движки модулів» (capability backends) в деталях вузла.
- Додано керування ролями жмого вузла (node roles) для core-вузла в деталях вузла.
- Переведено форматування дат на єдиний `VertepDatePipe` у Alerts, Workers, Worker Detail, Settings, Queue.
- Додано моделі `StoryboardVersion`, `StoryboardScene`, `NodeUpdateState` до фронтенду.
- Додано API сторібордів (`approveStoryboard`, `rejectStoryboard`, `regenerateStoryboard`).
- Виправлено E2E тести: `exact=True` для навігаційних посилань, CSS-селектор для Workers, видалення ручного runner.

## ПРАВИЛЬНА НАЗВА: 0.0.1.26
- Додано модуль сторібордів: `StoryboardService`, `StoryboardConflict`, генерація промптів через LLM, рендеринг у Telegram з inline-клавіатурою, REST API `/api/storyboards`.
- Розширено `JobStatus` новими станами: `STORYBOARD_QUEUED`, `STORYBOARD_GENERATING`, `STORYBOARD_PENDING_APPROVAL`, `STORYBOARD_REVISION_REQUESTED`.
- Перероблено `pipeline.prepare_job`: затверджений сторіборд використовується як сценарій без повторної генерації LLM, нормалізація викликається до початку циклу ретраїв.
- Переведено Web UI V2 маршрути на lazy-завантаження (`loadComponent`) для зменшення первинного бандлу.
- Додано компонент Setup (`setup.component`) з моделями `SetupStatus`, `SetupHealth`, `SetupCompleteResult`.
- Виправлено реактивність Workers: `loading` замінено на `loading()` signal для коректного оновлення DOM.
- Покращено Brands: додано обробник `channelChanged` для оновлення списку каналів після зміни.
- Додано `description` до `BackupInfo`, розширено моделі Update Center.
- Інтегровано `ToastService` та `ConfirmService` у Settings для зворотного зв'язку користувача.
- Додано змінні середовища для сторібордів: `OLLAMA_STORYBOARD_MODEL`, `OLLAMA_STORYBOARD_TIMEOUT`, `OLLAMA_STORYBOARD_MAX_RETRIES`, `STORYBOARD_TARGET_DURATION`.
- Покращено Browser E2E тести: додано стандартний мок `/api/status`, нові тести для сторібордів та setup.
- Додано unit-тести сторібордів (`test_storyboard.py`).
- Оновлено план завершення Web UI V2.

## ПРАВИЛЬНА НАЗВА: 0.0.1.25
- Видалено `test_web_ui_contracts.py` з git tracking: файл потребує локального CONFIG_ROOT і ADMIN_PASSWORD, несумісний з CI без додаткового налаштування.

## ПРАВИЛЬНА НАЗВА: 0.0.1.24
- Виправлено contract tests для CI: додано `auth=_auth()` для endpoint що потребують авторизації при наявності `ADMIN_PASSWORD`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.23
- Типізовано `NodeDetail.hardware` та `NodeDetail.runtime` через `WorkerHardware`/`WorkerRuntime` замість `Record<string, unknown>`.
- Розширено `NodeAction.action` regex: додано `update`, `rotate`, `revoke`.
- Додано state guards для Worker controls (`availableActions` computed): drain/resume/quarantine/disable/enable/restart/update залежно від статусу вузла.
- Покращено Roles deployment progress: `RolesDeploymentState`/`RolesUpdateResponse` моделі, polling після save, показ services per role, deployment error/status у Settings.
- Додано contract tests для V2C-401–V2C-404: registration token TTL, node detail hardware, worker actions, roles deployment shape.
- Додано Browser E2E тести для worker wizard, worker detail hardware/actions, settings roles deployment status.
- Оновлено план завершення Web UI V2: V2C-401–V2C-404 позначено DONE.


## ПРАВИЛЬНА НАЗВА: 0.0.1.22
- Відновлено складання Web UI V2: компонент Logs тепер входить до Git і підключений до реального `GET /api/logs` із фільтрами та станами завантаження, помилки й порожнього результату.
- Уточнено TypeScript-модель запису логу відповідно до фактичного backend contract.
- Усунуто неоднозначність Browser E2E для версій Update Center за допомогою стабільних `data-testid` без послаблення перевірки значень.
- Додано актуальний план завершення Web UI V2 на основі аудиту commit `3255a7a5` із чесними статусами, залежностями та acceptance criteria.

## ПРАВИЛЬНА НАЗВА: 0.0.1.21
- Реалізовано Settings V2 секції: Сховище секретів (V2-501), Моделі Ollama (V2-502), Центр оновлень з rolling update та canary (V2-503), Бекапи/відновлення (V2-504), Інтеграції та сертифікати (V2-505).
- Додано API методи: secrets CRUD, models pull/delete, update check/install/restart/readiness/rolling, backups list/create/restore, integrations status, certificates renew.
- Розширено моделі: SecretStatus, IntegrationStatus, ModelInfo, BackupInfo, UpdateReadiness, RollingStatus.
- Всі 258 unit-тестів та 13 browser E2E тестів проходять.

## ПРАВИЛЬНА НАЗВА: 0.0.1.20
- Створено компонент Job Detail: перегляд усіх полів Job (ID, тема, статус, пріоритет, дата, персонаж, workflow, scenes, artifacts, історія подій).
- Додано кнопку "Редагувати" у Job Detail: зміна теми, пріоритету, workflow; кнопку "Видалити" з confirm-діалогом.
- Додано `VertepDatePipe` — єдиний формат дати `дд.мм.рррр год:хв` для всього Web UI.
- Виправлено Dashboard статуси завдань: підрахунок з реальних jobs через централізовані `JOB_STATUS_GROUPS`.
- Покращено Dashboard "Архітектура системи": показує CORE + модулі (LLM, TTS, GPU, FFmpeg, Telegram) + Worker nodes з ролями та capabilities.
- Додано real-time ресурси (CPU, RAM, Disk) через `psutil` у `/api/status`; Dashboard показує "Недоступно" замість 0% при відсутності даних.
- Замінено raw JSON у Settings → Система на user-friendly таблицю з state, версією, компонентами, кнопкою "Показати технічні деталі".
- Додано 8 нових browser E2E тестів для перевірки Job Detail, статусового підрахунку, ресурсів, System settings, версії update subsystem, архітектури.

## ПРАВИЛЬНА НАЗВА: 0.0.1.19
- Виправлено оновлення Dashboard, Jobs, Characters і Settings у zoneless Angular: асинхронний UI state переведено на `signal()`, тому дані після HTTP-відповідей гарантовано відображаються в DOM.
- Системний ідентифікатор нового персонажа тепер генерується автоматично, залишається незмінним під час редагування та недоступний для ручної зміни.
- Відновлено empty state списку завдань і відображення `providers` та статусу оновлення у Settings; вилучено тимчасові `[DIAG]` логи.
- Уточнено Browser E2E locator кнопки редагування персонажа та додано перевірку відсутності JavaScript-помилок у Settings.


## ПРАВИЛЬНА НАЗВА: 0.0.1.18
- Додано діагностичні `console.log` у `ApiService`, `Dashboard` і `main.ts` для трасування даних між HTTP request і component state.
- Оновлено `tests/test_browser_e2e.py` під фактичний Web UI V2: тести використовують Angular routes `/`, `/jobs`, `/workers`, `/characters`, `/settings` та `data-testid` селектори; видалено залежність від застарілих V1 елементів `#nav button[data-panel=...]`, `#health`, `#jobs`, `#characters` тощо.
- Додано `data-testid` у V2 компоненти (`dashboard`, `jobs`, `workers`, `characters`, `settings`) для стабільних E2E селекторів.
- Видалено/адаптовано тестовий сценарії, що перевіряли відсутні у V2 сторінки Queue/Brands/Updates; збережено перевірки empty state, forms, navigation та settings backends.

## ПРАВИЛЬНА НАЗВА: 0.0.1.17
- Виправлено API contract V2: `api.service.ts` тепер нормалізує відповіді backend під фактичні поля — `job_id` → `id` для jobs, `workers` завжди масив, `load: 0` як fallback.
- Оновлено `SystemStatus` у V2: додано поля `version`, `ollama`, `update`, `workers` щоб відповідали чинному `/api/status`.
- Виправлено Dashboard: секція ресурсів показує «Дані про ресурси недоступні» якщо backend не повертає `resources`; додано null-guards.
- Виправлено Settings: додано `timeout(10000)` і `take(1)` щоб уникнути нескінченного loading; `backendSlots()` коректно обробляє відсутні `providers`.
- Виправлено Workers: фільтрація за `node_name`/`node_id` тепер безпечна при null значеннях.

## ПРАВИЛЬНА НАЗВА: 0.0.1.16
- Додано `POST /api/characters` у `core/api/resources.py` для створення персонажів; раніше фронтенд отримував 405 при спробі створити нового персонажа.
- Виправлено синхронізацію версії: `/api/status.update.current_version` тепер завжди дорівнює `/api/status.version` через `application_version()` у `core/update_manager.py`.
- Додано поле `version` до `SystemStatus` у `web-v2/src/app/core/models.ts` для передачі версії runtime у Web UI.
- Виправлено V2 render-path: додано null-guards у `jobs.component.ts` та `characters.component.ts` при пошуку, щоб відсутні `id`/`name` не ламали вкладки.
- Оновлено sidebar: замість хардкодного `Vertep Admin v2` тепер показує `Vertep v<runtime_version>` з `/api/status`; при недоступності API показує `...`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.15
- Покращено команди `vertep start`, `vertep stop`, `vertep restart` у `scripts/vertep`: перевірка exit code Docker Compose, показ активних контейнерів перед зупинкою, health-check циклом до 60 с після запуску з прогресом, `docker compose ps` після старту і зупинки, `[ПОМИЛКА]`/`[УВАГА]` при збоях.
- Виправлено `tests/test_update_security.py` і `tests/test_local_contracts.py`: виклики `read_text()` для `scripts/vertep` доповнено `encoding="utf-8"` щоб уникнути `UnicodeDecodeError` на Windows; оновлено assert під новий текст повідомлення запуску.


- Розширено `Content-Security-Policy` у `core/app.py`: додано `img-src 'self' data:` щоб уникнути блокування inline-SVG favicon із `web/index.html` (CSP `default-src 'self'` інакше забороняє `data:` URI для зображень).

## ПРАВИЛЬНА НАЗВА: 0.0.1.13
- Видалено CDN `cdn.tailwindcss.com` та inline `tailwind.config` з `web-v2/src/index.html`; Tailwind CSS 4 працює виключно через локальний PostCSS build.
- Виправлено `web-v2/src/styles.css`: замінено `@tailwind` директиви на `@import "tailwindcss"` (Tailwind 4 синтаксис), додано `@theme` з кастомними кольорами Vertep.
- Виправлено `app.component.ts`: замінено пряме монтування `LayoutComponent` на `<router-outlet>`, що відновлює роботу Angular Router і `AuthGuard`.
- Виправлено `AuthGuard`: використовує `getSession()` замість `getStatus()` для перевірки авторизації.
- Виправлено `ConfirmService`: логіка `confirm()` переписана через коректний `Observable` lifecycle — підписники тепер отримують значення.
- Переписано `ConfirmDialogComponent`: підключається до `ConfirmService.open$`, рендериться через `@if`, підтримує dark mode.
- Додано `SidebarService` з Angular signal для керування станом collapse sidebar.
- Додано `ThemeService` з `localStorage` persistence, ініціалізацією до рендеру та `dark` класом на `<html>`.
- Оновлено `SidebarComponent`: підтримує collapse (`w-64`/`w-16`), mobile overlay, `routerLinkActive`, dark mode.
- Оновлено `HeaderComponent`: toggle sidebar, dark mode toggle, system state badge (оновлюється кожні 30 с), коректне визначення title за URL, logout.
- Оновлено `LayoutComponent`: підключено `SidebarService`, `ConfirmDialogComponent`, mobile backdrop.
- Додано wildcard route `{ path: '**', redirectTo: '' }` в `app.routes.ts`.
- Розширено Playwright smoke tests: перевірка відсутності CDN, відсутності `NG04002`/`ReferenceError` у консолі, перевірка всіх маршрутів.

## ПРАВИЛЬНА НАЗВА: 0.0.1.12
- Винесено спільні допоміжні функції job у `core/api/job_helpers.py`, маршрути керування job — у `core/api/jobs.py`, обробку task — у `core/api/tasks.py`, керування worker — у `core/api/workers.py`.
- Винесено маршрути реєстрації та керування вузлами у `core/api/nodes.py`, а перевірки стану, метрики, сповіщення, журнали та обслуговування — у `core/api/observability.py`.
- Додано заготовки модулів `core/api/system.py` і `core/api/telegram.py` та `TelegramServiceHolder` у `core/state.py`; робочі системні й Telegram маршрути та життєвий цикл сервісу залишаються у `core/app.py`.
- Додано документацію рефакторингу `docs/refactoring/large-files-refactoring.md` з картою модулів і описом життєвого циклу Telegram.
- Відновлено сумісність публічних імпортів `core.app`, збережено поведінку маршрутів вузлів і моніторингу та усунуто дублювання маршрутів; додано перевірки унікальності API-маршрутів і переходу вузла в `DRAINING`.
- Об’єднано незакомічені етапи рефакторингу в одну версію `0.0.1.12`; узгоджено `VERSION`, `CHANGELOG.md` та нотатки релізу.

## ПРАВИЛЬНА НАЗВА: 0.0.1.11
- Продовжено рефакторинг `core/app.py`: винесено first-run/setup домен у `core/api/setup.py` (3 endpointe: `/api/setup`, `/api/setup/health`, `/api/setup/complete`) разом із хелпером `_validate_ai_backend`. Додано re-export `first_run_complete`/`_validate_ai_backend` у `core.app` для збереження публічного контракту. `app.py` зменшено до ~2159 рядків. API-контракт та тести незмінні: 256 passed, 9 skipped.

## ПРАВИЛЬНА НАЗВА: 0.0.1.10
- Продовжено рефакторинг `core/app.py`: винесено домен керування моделями у `core/api/models.py` (5 endpointe: `/api/models/text`, `/api/models/text/pull`, `/api/models/text/{model}`, `/api/models/voices`, `/api/models/voices/synthesize`). `app.py` зменшено до ~2283 рядків. API-контракт та тести незмінні: 256 passed, 9 skipped.

## ПРАВИЛЬНА НАЗВА: 0.0.1.9
- Розпочато системний рефакторинг великих файлів. `core/app.py` зменшено з 2576 до ~2320 рядків шляхом винесення доменів у окремі модулі без зміни API-контракту та публічних імпортів:
  - `core/security.py` — шар автентифікації/авторизації (хелпери `_hash_secret`, авторизації Web UI); пере-експортований у `app.py`.
  - `core/state.py` — спільний mutable-стан (`store`, `executor`, `task_queue`, `workflow_registry`, `request_windows`, `result_locks`, telegram pending dicts); пере-експортований у `app.py`.
  - `core/api/workflows.py` — роутер CRUD `/api/workflows` (4 endpointe).
  - `core/api/resources.py` — роутер characters/brands/channels (13 endpointe).
  - `core/api/settings.py` — роутер settings/integrations/logo (7 endpointe).
- Поведінка та URL незмінні (перевірено: 101 маршрут, жодного відсутнього). Циклічних імпортів немає. Тести: 256 passed, 9 skipped.

## ПРАВИЛЬНА НАЗВА: 0.0.1.8
- Виправлено URL у browser-e2e тестах: додано `V1_URL` з суфіксом `/v1` для всіх переходів `page.goto()`, оскільки Web UI v2 тепер за замовчуванням на `/`, а класичний v1 — на `/v1`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.7
- Виправлено падіння `core` при старті в Docker: у `Dockerfile` додано `COPY publishers publishers`. Раніше пакет `publishers` не потрапляв в образ, але `core/app.py` → `core/pipeline.py` → `adapters/providers/__init__.py` → `compute_backends.py`/`video_engines.py` мають безумовний імпорт `from publishers.transport import HttpTransport`, що викликало `ModuleNotFoundError: No module named 'publishers'` і аварійний вихід контейнера `vertep-core-1`.
- Оновлено версії GitHub Actions у всіх workflows: `actions/checkout` → v7, `actions/setup-python` → v7, `actions/upload-artifact` → v7, `actions/download-artifact` → v8.
- У `.github/workflows/browser-e2e.yml` додано крок збірки Angular фронтенду (`setup-node@v4`, Node 24, `npm ci && npm run build`) до запуску E2E-смоук-тестів, що усуває залежність від скомпільованої збірки в репозиторії.
- У `ci.yml` і `release.yml` для артефактів кваліфікації змінено `if-no-files-found: error` → `warn`.
- У `scripts/qualify-release.py` обгорнуто три оголені виклики `.read_text()` (для `deploy/docker-compose.yml`, `bootstrap.sh`, `deploy/proxy.conf`) у `try/except OSError`, щоб перевірка не падала з необробленим винятком за відсутності файла.

## ПРАВИЛЬНА НАЗВА: 0.0.1.6
- Виправлено монтування Web UI v2 у `core/app.py`: v2-маунт тепер виконується лише за наявності скомпільованої збірки Angular (`web-v2/dist`); інакше з кореня сервується класична v1. Раніше `web-v2/dist/` було закомічено в git, що маскувало жорстку залежність імпорту `core.app` від статичних файлів. Після виключення build-артефактів з трекінгу імпорт падав у CI з `RuntimeError: Directory 'web-v2/dist/vertep-admin-v2' does not exist`, що блокувало збирання тестів (`pytest`).

## ПРАВИЛЬНА НАЗВА: 0.0.1.5
- Впроваджено Provider / Adapter Layer (фази 1–6 open-source аудиту): формалізовані інтерфейси та лазливий registry `providers` у `adapters/providers/`.
- Переведено `core/pipeline.py`, `core/script_agent.py`, `worker/service.py`, `worker/role_executor.py` на `providers.*` та абстрактні інтерфейси; ffmpeg-збірку через `engine.render()` у `finalize_job`.
- Додано LLM-клієнти Ollama та OpenAI-сумісний (`adapters/llm_clients.py`, `VERTEP_LLM_PROVIDER`).
- Додано open-source TTS-движки Piper та Kokoro (`adapters/providers/tts_backends.py`, `TTS_PROVIDER`).
- Створено live Publisher-адаптери YouTube/TikTok/Facebook/Instagram/Threads на транспортному шарі (`publishers/base.py`, `publishers/transport.py`, `LIVE_PUBLISHERS`), інтегровано через `providers.publisher()`.
- Додано опційний ComfyUI-Distributed compute (`ComfyUIDistributedProvider`) з fallback на `vertep-worker`.
- Додано VideoEngine: `native` (FFmpeg) за замовчуванням, опційні `money-printer` / `shortgpt` (`VERTEP_VIDEO_ENGINE`).
- Додано `provider_matrix()` та інтегровано в `/api/status` (поле `providers`).
- Web UI v2 тепер за замовчуванням на `/`, класичний v1 на `/v1` (SPA-fallback та перемикач у header).
- У Налаштуваннях додано панель «Движки обробки (backends)».
- Оновлено `AGENTS.md` (розділ 28), `README.md`, `.env.example`; додано `docs/architecture/open-source-audit.md`.
- Додано `web-v2/dist/` та `web-v2/.angular/` до `.gitignore` і прибрано build-артефакти з git-трекінгу.
- Додано тести: `test_providers.py`, `test_llm_tts_providers.py`, `test_publisher_live_adapters.py`, `test_compute_distributed.py`, `test_video_engines.py`, `test_provider_matrix.py`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.4
- Виправлено кореневий `.gitignore`: шаблон `jobs/` був змінений на `/jobs/`, бо він необґрунтовано ігнорував `web-v2/src/app/jobs/` на будь-якій глибині, через що `jobs.component.ts` не потрапляв у git і `ng build` падав з `TS2307: Cannot find module './jobs/jobs.component'` (код 1).

## ПРАВИЛЬНА НАЗВА: 0.0.1.3
- Оновлено Node-образ у `web-v2` етапі `Dockerfile` з `node:20-slim` на `node:24-slim`: Angular CLI v22 вимагає Node.js ≥ v22.22.3 / v24.15.0, через що `ng build` завершувався з кодом 3 і збірка образу `core` падала.

## ПРАВИЛЬНА НАЗВА: 0.0.1.2
- Додано `.dockerignore` з виключенням `**/node_modules`: раніше крок `COPY web-v2/ .` перезаписував свіжі node_modules від `npm ci` на закомічені з Windows, через що `ng` втрачав exec-біт і збірка образу `core` падала з `Permission denied` (код 127).
- Додано `node_modules/` та `web-v2/node_modules/` до `.gitignore`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.1
- Виправлено тест `tests/test_release.py::test_runtime_version_comes_from_version_file`, який жорстко очікував префікс версії `0.0.0.` і блокував release workflow після переходу на нову послідовність версій `0.0.1.x`.

## ПРАВИЛЬНА НАЗВА: 0.0.1.0
- Переведено збірку образу на багатоетапний `Dockerfile`: додано окремий `web-v2` Node-етап з `npm ci` та `npm run build`.
- Змонтовано статичний Web UI v2 (Angular) за шляхом `/admin` у `core/app.py`.
- Додано спільний модуль атомарного запису файлів `core/atomic_write.py` та переведено `core/update_manager.py` на `atomic_write_json`.
- Додано автентифікацію Web UI: `AuthGuard`, сторінку входу `login`, класовий `AuthInterceptor` з редиректом на `/login` за відповіді 401, новий `api.service` замість `vertep-api.service`.
- Додано сервіси `toast` і `confirm`, i18n-пайп `uk`, компоненти `confirm-dialog` та `toast-container`.
- Оновлено маршрутизацію (`canActivate`, title сторінок) та провайдерів у `app.config.ts`.
- Реалізовано CRUD персонажів: створення, оновлення та видалення.
- Оновлено `web-v2/package.json` (додано `@playwright/test`, скрипти `build`/`start`/`test:e2e`), `web-v2/angular.json` (serve) та `web-v2/postcss.config.js` (`@tailwindcss/postcss`).
- Додано Playwright e2e тести для Web UI.

## ПРАВИЛЬНА НАЗВА: 0.0.0.99
- Додано Web UI v2 на Angular: каркас застосунку, роутинг, layout зі sidebar/header, сторінки Dashboard, Jobs, Workers, Characters, Settings.
- Додано API-сервіс, auth interceptor та моделі даних (Character, Worker тощо).
- Оновлено release workflow.

## ПРАВИЛЬНА НАЗВА: 0.0.0.98
- Виправлено Web UI Dashboard: додано favicon, прибрано залежність від `/api/settings/logo` (404), версія тепер завантажується з `/api/status` замість хардкоду `v1.3.0`.
- Виправлено навігацію Sidebar: усі пункти мають унікальний `data-panel`, додано відсутні панелі (`characters`, `brands`, `workflows`, `queue`, `errors`), замінено `<a>` на `<button>`.
- Виправлено `refresh()`: переписано на `Promise.allSettled`, додано null-guards для DOM-операцій, один помилковий widget не блокує весь Dashboard.
- Виправлено `insertAdjacentHTML` null error: додано перевірки перед динамічними вставками.
- Виправлено дублікати `id` у HTML: унікалізовано форми та діалоги.
- Додано `#health` елемент у header, заповнюється станом Core з `/api/status`.
- Виправлено release-систему: додано `gh_run`, `gh_run_json`, `verify_release`, `orchestrate_release` у `scripts/release.py`.
- Оновлено `.github/workflows/release.yml`: додано step генерації `qualification.json` через `scripts/qualify-release.py`.
- Відновлено проходження `tests/test_browser_e2e.py`: 9 passed.
- Відновлено проходження `tests/test_release.py`: 20 passed.

## ПРАВИЛЬНА НАЗВА: 0.0.0.97
- Виправлено Web UI Dashboard відповідно до затвердженого референсу:
- Виправлено критичну JS-помилку `Cannot set properties of null (setting 'onclick')` — замінено `$("#nav").onclick` на event delegation.
- Виправлено `Assignment to constant variable` — `const renderDashboard` → `let`.
- Вилучено 48 дублікатів `id` у HTML (видалено 5 дубльованих діалогів).
- Sidebar: одна вертикальна колонка, фіксована ширина, вертикальний скрол, без горизонтального overflow.
- Палітра: видалено бежево-кремову, додано холодний нейтральний світлий фон (#F8FAFC), білі картки, зелений primary (#10B981).
- Додано CSS design tokens (--bg-page, --bg-surface, --primary, --success, --info, --warning, --danger, --purple тощо).
- Header: Dashboard/Огляд ліворуч, статус/нотифікації/допомога/профіль праворуч.
- KPI: 6 карток (Воркери, Активні завдання, Завдань у черзі, Навантаження, GPU, Стан).
- Architecture: CORE + 6 груп вузлів з онлайн-лічильниками.
- Job Status: donut chart + легенда.
- Resources: CPU/RAM/Диск progress bars без бежевих відтінків.
- Workers table: 8 колонок, pill-статуси, контекстне меню.
- Right Sidebar: System State, Recent Activity, Quick Actions, License.
- Responsive: брейкпойнти 1320/1100/760px.
- Виправлено `scripts/release.py`: додано повну оркестрацію `реліз` (`--release`): пуш при брудному дереві, `gh workflow run`, polling очікування, верифікація tag/release/артефактів.
- Додано тести для `реліз`: пуш→workflow, чисте дерево, failed workflow, verify_release з dereferencing annotated tag.

## ПРАВИЛЬНА НАЗВА: 0.0.0.96
- Виправлено `scripts/release.py`: `release_changelog()` тепер створює блоки у форматі `## ПРАВИЛЬНА НАЗВА: <версія>` замість `## <версія> — <дата>`.
- Додано remote tag discovery через `git ls-remote --tags origin` у `known_versions()`.
- Додано push до `main` у `scripts/release.py` після створення commit, з перевіркою SHA.
- Видалено параметр `--title` з `scripts/release.py`: commit subject тепер лише `<VERSION>`.
- Зроблено `git fetch --tags origin` необов'язковим у `prepare_release()`.
- Спрощено формат `releases/<VERSION>.md`: лише `# Vertep <VERSION>` та український опис змін.
- Виправлено `check_release()` у `scripts/release.py`: перевіряє перший рядок `releases/<VERSION>.md` окремо від решти вмісту.
- Додано `errors="replace"` до subprocess для коректної роботи з кирилицею в PowerShell.
- Виключено `tests/test_browser_e2e.py` з перевірок у `prepare_release()` та workflow (AGENTS.md §21: `ERR_CONNECTION_REFUSED` допустимий).
- Додано тести для нових форматів CHANGELOG, release notes та read-only `--check`.
- Оновлено `README.md`: прибрано `--title`, оновлено інструкції щодо `release.py`.
- Оновлено `.kilo/rules/release-rules.md`: формат коміту `<version>` замість `<version> — <description>`, додано push у `main` у `release.py`.
- Оновлено `AGENTS.md` розділ 3: формалізовано команди `пуш` та `реліз`, їхнішні алгоритми та обмеження.

## ПРАВИЛЬНА НАЗВА: 0.0.0.95
- Додано новий Dashboard за технічним завданням: 3-колоночна структура, світла мінімалістична тема, лівий sidebar з навігацією, центральна область з KPI/архітектурою/ресурсами/таблицею воркерів, правий sidebar з активністю/швидкими діями/ліцензією.
- Додано KPI-картки: Воркери, Активні завдання, Завдань у черзі, Навантаження системи, Використання GPU.
- Додано блок Архітектура системи з CORE + групами вузлів (GPU/Text/Voice/Publisher/Backup/Monitoring) та фільтрацією.
- Додано donut chart статусів завдань, прогрес-бари ресурсів (CPU/RAM/Диск), таблицю Worker з контекстним меню `⋮`.
- Додано детальні widgets для режимів Maintenance/Update на Dashboard.
- Додано profile dropdown, notification dropdown, help button.
- Додано підтримку логотипу: завантаження через Settings, серверне збереження через `/api/settings/logo`.
- Додано повне контекстне меню Worker: Відкрити, Drain, Disable, Restart Service, Update, Health Check, View Logs, Remove.
- Додано фільтрацію архітектури за роллю та статусом (Offline/Error) з автопереходом на Workers.
- Розширено `/api/nodes/{node_id}/actions`: додано дії `disable`, `enable`, `restart`, `update`, `logs`.
- Розширено `/api/logs` фільтром `node_name`.
- Додано `/api/settings/logo` GET/PUT/DELETE для збереження логотипу dashboard.
- Оновлено `/api/system/license` проксіювання.
- Оновлено UI логіку: real-time оновлення, skeleton loading, empty/error states, адаптивність.

## ПРАВИЛЬНА НАЗВА: 0.0.0.94
- Виправлено логіку створення релізу

## ПРАВИЛЬНА НАЗВА: 0.0.0.93
- Виправлено логіку створення релізу
- нові правила агентам

## ПРАВИЛЬНА НАЗВА: 0.0.0.92
- Виправлено логіку створення релізу
- нові правила агентам

## ПРАВИЛЬНА НАЗВА: 0.0.0.91
- Виправлено логіку створення релізу

## ПРАВИЛЬНА НАЗВА: 0.0.0.90
- Додано привілейовану дію перезапуску сервера в адміністративній панелі та забезпечено автоматичне відтворення кожного активного сервісу ролі Core при оновленні, залишаючи PostgreSQL і Redis запущеними.
- Додано український п'ятиетапний вигляд оновлення з живим відображенням прогресу у відсотках, читабельними повідомленнями поточного кроку, обробкою тимчасового перезапуску та автоматичним оновленням сторінки після успіху.
- Збережено детальний прогрес, генерований workflow оновлення хоста, в кінцевій історії оновлень та додано покриття API, executor, контракту та браузера для операцій перезапуску.
- Виправлено `scripts/release.py`: коміт релізу тепер має бути просто `<версія>`, без дефісу та опису. Оновлено валідацію `check_release()`.
- Постійне зберігання налаштувань Telegram: `TELEGRAM_ALLOWED_CHAT_IDS` та `TELEGRAM_ADMIN_CHAT_IDS` тепер зберігаються в `config/telegram-settings.json` з permissions `0600` замість `os.environ`, що запобігає втраті налаштувань після перезапуску.
- Додано можливість вибору персонажа у Telegram: після надсилання теми користувач спочатку обирає бренд, а потім персонажа. Задача створюється лише після вибору персонажа. Якщо персонажів немає, використовується персонаж за замовчуванням.
- Приховано токен Telegram Bot у Web UI: поле вводу токену тепер відображає `••••••` коли токен вже налаштований.
- Додано індикатор черги ролей у Web UI: коли `deployment-request.json` створено, відображається статус "Заявка в черзі на застосування ролей...".

## ПРАВИЛЬНА НАЗВА: 0.0.0.89
- Додано `AGENTS.md` — єдине правило для агентів проєкту: мова, версіонування, архітектура, сценарний агент, ComfyUI executor, FFmpeg пайплайн, Bootstrap/First Run Wizard, Safe Update System, тестування, безпека.
- Реалізовано ComfyUI executor у `worker/role_executor.py`: додано `execute_image()`/`execute_video()`, зареєстровано в `EXECUTORS`/`ROLE_TASKS`.
- Уніфіковано `worker/service.py`: тепер всі задачі викликаються через `execute_role_task()`, включаючи `image`/`video`.
- Створено `core/script_agent.py` — багатоступінчастий сценарний агент: структура → плани сцен → деталізація кожної сцени.
- Оновлено `core/pipeline.py`: переведено на `ScriptAgent`, передача конфігу персонажа, fallback на перегенерацію окремої сцени.
- Оновлено `tests/test_features.py`: замінено `LLMAdapter` на `ScriptAgent` у monkeypatch.
- Додано сценарний агент в `AGENTS.md`: багатоступінчаста генерація, fallback, нормалізація через `ScriptDocument`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.88
- Додано `AGENTS.md` — єдине правило для агентів проєкту: мова, версіонування, архітектура, сценарний агент, ComfyUI executor, FFmpeg пайплайн, Bootstrap/First Run Wizard, Safe Update System, тестування, безпека.
- Реалізовано ComfyUI executor у `worker/role_executor.py`: додано `execute_image()`/`execute_video()`, зареєстровано в `EXECUTORS`/`ROLE_TASKS`.
- Уніфіковано `worker/service.py`: тепер всі задачі викликаються через `execute_role_task()`, включаючи `image`/`video`.
- Створено `core/script_agent.py` — багатоступінчастий сценарний агент: структура → плани сцен → деталізація кожної сцени.
- Оновлено `core/pipeline.py`: переведено на `ScriptAgent`, передача конфігу персонажа, fallback на перегенерацію окремої сцени.
- Оновлено `tests/test_features.py`: замінено `LLMAdapter` на `ScriptAgent` у monkeypatch.
- Додано сценарний агент в `AGENTS.md`: багатоступінчаста генерація, fallback, нормалізація через `ScriptDocument`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.87
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.87 — Виправлення бренду, Telegram статусу, конструктор сценарію
- Виправлено збереження бренду: видалено неіснуючий елемент `brand-channels` з тіла PUT-запиту, через який збереження падало.
- Виправлено перевірку Telegram: `checkTelegramBot()` тепер показує реальний статус polling (`running` / `error` / `stopped`) замість фіксованого `RUNNING`.
- Замінено raw JSON textarea сценарію на дружній конструктор сцен: поля для заголовка, опису, хештегів, озвучення, список сцен з можливістю додавати/видаляти.
- Виправлено поведінку журналу оновлення: стан `<details>` тепер зберігається між оновленням snapshot.

## ПРАВИЛЬНА НАЗВА: 0.0.0.86
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.85 — Виправлення бренду, Telegram статусу, конструктор сценарію
- Виправлено збереження бренду: видалено неіснуючий елемент `brand-channels` з тіла PUT-запиту, через який збереження падало.
- Виправлено перевірку Telegram: `checkTelegramBot()` тепер показує реальний статус polling (`running` / `error` / `stopped`) замість фіксованого `RUNNING`.
- Замінено raw JSON textarea сценарію на дружній конструктор сцен: поля для заголовка, опису, хештегів, озвучення, список сцен з можливістю додавати/видаляти.
- Виправлено поведінку журналу оновлення: стан `<details>` тепер зберігається між оновленням snapshot.

## ПРАВИЛЬНА НАЗВА: 0.0.0.85
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.84 — Виправлення Telegram polling та publisher-worker
- Виправлено publisher-worker: health-check тепер не падає з `PermissionError`, коли каталог storage недоступний для запису.
- Додано long polling для Telegram: мігруємо з webhook на постійний опитування оновлень.

## ПРАВИЛЬНА НАЗВА: 0.0.0.84
- Виправлено publisher-worker: health-check тепер не падає з `PermissionError`, коли каталог storage недоступний для запису.
- Додано long polling для Telegram: мігруємо з webhook на постійний опитування оновлень.

## ПРАВИЛЬНА НАЗВА: 0.0.0.83
- Оновлено `VERSION` (проміжний bump без змісту коду).

## ПРАВИЛЬНА НАЗВА: 0.0.0.82
## ОРИГІНАЛЬНА НАЗВА: Merge branch 'main' of https://github.com/fylypovych/vertep
- Додано файл `releases/0.0.0.78.md` (12 рядків) у результаті злиття гілки `main` віддаленого репозиторію.

## ПРАВИЛЬНА НАЗВА: 0.0.0.81
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.80
- Оновлено `CHANGELOG.md` (8 доданих рядків) та `VERSION` (bump до 0.0.0.80) як частина підготовки релізу 0.0.0.80.

## ПРАВИЛЬНА НАЗВА: 0.0.0.80
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.78 — Виправлення Telegram polling та publisher-worker
- Додано `releases/0.0.0.78.md` (12 рядків) та оновлено `VERSION` і `CHANGELOG.md` для фіксації релізу 0.0.0.78.

## ПРАВИЛЬНА НАЗВА: 0.0.0.79
## ОРИГІНАЛЬНА НАЗВА: Migrate Telegram integration from webhook to long polling
- Додано `TelegramPollingService` з персистенцією offset, обробкою `retry`/`429` та graceful shutdown.
- Інтегровано polling у lifespan додатку з graceful shutdown.
- Додано обробник команди `/start` (для webhook і polling шляхів).
- Оновлено `telegram_status`: тепер звітує про polling-статус, bot username, last update.
- Оновлено UI налаштування Telegram: polling-контроли, перевірка бота, збереження chat ID.
- Додано тести для персистенції polling offset, обробника `/start`, збереження chat ID.

## ПРАВИЛЬНА НАЗВА: 0.0.0.78
## ОРИГІНАЛЬНА НАЗВА: Fix publisher-worker unhealthy by handling PermissionError in health check
- Виправлено publisher-worker: health-check тепер не падає з `PermissionError`, коли каталог storage недоступний для запису (publisher-worker працює як uid 10001 з read-only root та host-mounted `./storage` від root; раніше `_root().mkdir()` кидав `PermissionError` → 500 → curl failed; тепер обробляється gracefully, healthcheck проходить).

## ПРАВИЛЬНА НАЗВА: 0.0.0.77
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.77 — Виправлено падіння publisher-worker та діагностику bootstrap
- Виправлено падіння publisher-worker: додано відсутню залежність `httpx` в імедж `docker/publisher`.
- Покращено діагностику bootstrap: при таймауті перевірки здоров'я виводиться, який саме сервіс не проходить перевірку, та останні логи контейнера.

## ПРАВИЛЬНА НАЗВА: 0.0.0.76
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.76 — Виправлено налаштування Telegram та діагностику bootstrap
- Додано поле введення токена Telegram безпосередньо в картці Telegram у веб-інтерфейсі.
- Покращено діагностику bootstrap: при таймауті перевірки здоров'я виводиться, який саме сервіс не проходить перевірку, та останні логи контейнера.

## ПРАВИЛЬНА НАЗВА: 0.0.0.75
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.75 — Додано веб-налаштування Telegram без CLI
- Додано веб-налаштування Telegram: токен, `PUBLIC_URL`, webhook secret, дозволені та адмінські чати.
- `TelegramAdapter` тепер читає токен із зашифрованого сховища інтеграцій, якщо змінна оточення відсутня.
- Додано `/api/telegram/status` для перевірки поточного стану webhook та конфігурації.
- `/api/telegram/setup` тепер приймає параметри через JSON-тіло та не вимагає перезапуску CORE.

## ПРАВИЛЬНА НАЗВА: 0.0.0.74
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.74 — Додано мультиканальну публікацію та Telegram-бота з затвердженням
- Додано мультиканальну публікацію: модель каналів, CRUD API, UI керування каналами в діалозі бренду.
- Telegram-бот тепер просить вибрати бренд при створенні завдання та відправляє затвердження в адмін-чати.
- Додано стан `PENDING_APPROVAL` для завдань, які чекають затвердження в Telegram.
- Після схвалення адмін може обрати канали для публікації або опублікувати всюди.
- Додано адаптер публікації в Telegram, а також методи `send_video` і `send_photo` в `TelegramAdapter`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.73
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.73 — Виправлено зависання інсталятора при додаткових ролях
- Виправлено зависання інсталятора при додаткових ролях: додано `tee` для потокового виводу під час `service_unit_check` та `enable_service` в `bootstrap.sh`, а також `set -x` діагностику; додано відповідний тест.

## ПРАВИЛЬНА НАЗВА: 0.0.0.72
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.72 — Дозволено безпечне оновлення systemd-служб
- Дозволено безпечне оновлення systemd-служб: `vertep-update.service` тепер може перезапускати інші захищені сервіси через `update-agent.py` без помилок `unit is masked` або `permission denied`; додано регресійні тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.71
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.71 — Виправлено оновлення host-CLI у захищеній службі
- Виправлено оновлення host-CLI у захищеній службі: `scripts/vertep` тепер доступний для `update-agent` без втрати `No such file or directory`; `scripts/status.py` використовує `Path(__file__).parent` замість hard-coded шляху; додано `set -euo pipefail` для стабільності; додано тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.70
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.70 — Виправлено аварійне завершення оновлення
- Виправлено аварійне завершення оновлення при недоступних сервісах: `update-agent.py` тепер перевіряє наявність сервісів перед зупинкою, коректно обробляє 502/503 від readiness, та не падає з `SystemctlError`; додано детальне логування; `scripts/vertep` додано `set -euo pipefail`; додано регресійні тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.69
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.69 — Завершено надійне керування сервісами та оновлення
- Додано `set -euo pipefail` у `scripts/vertep` для безпечного виконання.
- Покращено `update_protocol.py` для надійного керування сервісами під час оновлення.
- Додано `releases/0.0.0.69.md` та оновлено `scripts/release.py` для нової release-ceremony.
- Додано регресійні тести для `scripts/vertep` та `core/update_protocol.py`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.68
- Додано `set -euo pipefail` у `scripts/vertep` для безпечного виконання.
- Покращено `update_protocol.py` для надійного керування сервісами під час оновлення.
- Додано `releases/0.0.0.69.md` та оновлено `scripts/release.py` для нової release-ceremony.
- Додано регресійні тести для `scripts/vertep` та `core/update_protocol.py`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.67
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.67 — Покращено керування сервісами та надійність оновлень
- Додано `set -euo pipefail` у `scripts/vertep` для безпечного виконання.
- Покращено `update_protocol.py` для надійного керування сервісами під час оновлення.
- Додано `releases/0.0.0.69.md` та оновлено `scripts/release.py` для нової release-ceremony.
- Додано регресійні тести для `scripts/vertep` та `core/update_protocol.py`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.66
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.66 — Виправлено перевірку здоров'я резервного копіювання
- Виправлено перевірку здоров'я резервного копіювання: `services/backup_service.py` тепер коректно перевіряє, чи процес backup виконується, та не повертає `HEALTHY` коли сервіс ще не готовий; додано тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.65
## ОРИГІНАЛЬНА НАЗВА: 0.0.0.65 — Виправлено формування та опис релізів
- Виправлено формування та опис релізів: об'єднано код, номер версії та український опис змін в одному основному коміті; релізний workflow більше не створює окремий коміт від бота й лише ставить тег на перевірений коміт.
- Додано обов'язкову перевірку української назви коміту, опису в `CHANGELOG.md` і нотаток релізу, а також виправлено пошкоджене кодування текстів генератора.
- Додано `releases/0.0.0.65.md` та оновлено `scripts/release.py`, `.github/workflows/release.yml`, `CHANGELOG.md` як частина підготовки релізу 0.0.0.65.

## ПРАВИЛЬНА НАЗВА: 0.0.0.64
- Додано `releases/0.0.0.65.md` та оновлено `scripts/release.py`, `.github/workflows/release.yml`, `CHANGELOG.md` як частина підготовки релізу 0.0.0.65.

## ПРАВИЛЬНА НАЗВА: 0.0.0.63
## ОРИГІНАЛЬНА НАЗВА: Improve recovery roles and admin editors
- Покращено recovery ролі: `core/first_run.py` тепер дозволяє вибирати декілька додаткових ролей при відновленні інсталяції.
- Покращено admin editors: `core/app.py` додано нові API для редагування ролей вузлів та конфігурації admin.
- Оновлено `config/node_roles.json` для підтримки recovery сценаріїв.
- Додано `releases/0.0.0.63.md` (відповідно до підготовки релізу).

## ПРАВИЛЬНА НАЗВА: 0.0.0.62
- Додано `releases/0.0.0.62.md` (13 рядків) та оновлено `VERSION` і `CHANGELOG.md` для фіксації релізу 0.0.0.62.

## ПРАВИЛЬНА НАЗВА: 0.0.0.61
## ОРИГІНАЛЬНА НАЗВА: Add guided update restart progress
- Додано керований прогрес перезапуску при оновленні: `core/app.py` отримав новий endpoint `/api/system/update/restart-progress` з покроковим звітуванням; `core/update_manager.py` тепер публікує етапи restart через події; `scripts/update-agent.py` додано детальне логування етапів та таймаутів; `scripts/vertep` додано обробку `restart-progress` команди.
- Додано `releases/0.0.0.61.md` (відповідно до підготовки релізу).

## ПРАВИЛЬНА НАЗВА: 0.0.0.60
- Додано привілейоване перезавантаження сервера в адміністративній панелі та забезпечено автоматичне відтворення кожного активного сервісу ролі Core при оновленні, залишаючи PostgreSQL і Redis запущеними.
- Додано український п'ятиетапний вигляд оновлення з живим відображенням прогресу у відсотках, читабельними повідомленнями поточного кроку, обробкою тимчасового перезапуску та автоматичним оновленням сторінки після успіху.
- Збережено детальний прогрес, генерований workflow оновлення хоста, в кінцевій історії оновлень та додано покриття API, executor, контракту та браузера для операцій перезапуску.

## ПРАВИЛЬНА НАЗВА: 0.0.0.59
- Додано API `/api/system/roles` для перегляду й конфігурації додаткових ролей вузла, оновлено `deployment_plan.py` для підтримки `additional_roles`, розширено `docker-compose.yml` змінними `NODE_ADDITIONAL_ROLES`/`SUPPORTED_TASKS`/`WORKER_REQUIRE_GPU` та додано локалізацію ролей у Web UI.

## ПРАВИЛЬНА НАЗВА: 0.0.0.58
- Виправлено зупинку оновлень адміністративної панелі перед встановленням через `Connection refused`: привілейований оновлювач хоста тепер використовує drain через відкритий HTTPS-проксі пристрою на порту 8443 замість невідкритого порту Core лише для контейнерів 8080, тоді як віддалені URL Core зберігають звичайну перевірку TLS.

## ПРАВИЛЬНА НАЗВА: 0.0.0.57
- Оновлено `update-agent.py` для підтримки HTTPS-з'єднання з локальним Core через самопідписаний сертифікат, збережено TLS-перевірку для віддалених адрес і додано відповідні тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.56
- Завершено українську локалізацію майстра підключення вузлів, включаючи його заголовок, назви ролей та пояснювальну термінологію; покриття браузера тепер захищає кожну назву ролі від регресій англійською мовою.

## ПРАВИЛЬНА НАЗВА: 0.0.0.55
- Розширено українську локалізацію Web UI: додано переклади для ролей вузлів та інших елементів, а також тест на коректність українських міток у майстрі додавання Worker.

## ПРАВИЛЬНА НАЗВА: 0.0.0.54
- Замінено сирий JSON-редактор персонажів на адаптивну українську форму для налаштувань ідентичності, мови, поведінки, зовнішності, голосу, генерації та публікації; редагування тепер завантажує поточного персонажа та відкривається стабільно.
- Локалізовано навігацію інформаційної панелі, дії, статуси та термінологію життєвого циклу для операторів українською, зберігаючи технічні ідентифікатори там, де вони потрібні для конфігурації.
- Додано покриття JavaScript-контракту та браузерні тести для створення та редагування персонажів без помилок у консолі.

## ПРАВИЛЬНА НАЗВА: 0.0.0.53
- Створено локалізовану форму редагування персонажа у Web UI (`admin-uk.css`/`admin-uk.js`), додано відповідні браузерні E2E-тести та оновлено існуючі тести на нову поведінку.

## ПРАВИЛЬНА НАЗВА: 0.0.0.52
- Виправлено фатальну синтаксичну помилку вбудованого JavaScript інформаційної панелі, відновивши навігацію, оновлення здоров'я та елементи керування життєвим циклом; CI тепер парсить кожен вбудований скрипт інформаційної панелі за допомогою Node.js.
- Bootstrap встановлює драйвер PostgreSQL для executor оновлення хосту та безпечно замінює будь-який запит у черзі, коли явний запуск bootstrap бере управління, запобігаючи `No module named psycopg` та застарілому стану `PENDING`.
- `vertep status` тепер використовує кероване середовище для ролі/назви вузла, виводить встановлену версію та показує прогрес оновлення замість повідомлення `VERTEP UNKNOWN`.
- Браузерні димові тести тепер перевіряють завантаження інформаційної панелі, навігацію JavaScript без помилок та публічний контракт здоров'я.

## ПРАВИЛЬНА НАЗВА: 0.0.0.51
- Оновлено браузерний E2E-тест: замість перевірки редіректу на First Run Wizard тепер перевіряється завантаження дашборду з навігацією та відсутністю JS-помилок.

## ПРАВИЛЬНА НАЗВА: 0.0.0.50
- Bootstrap тепер перевіряє активність `vertep-update.service` перед встановленням, скасовує заблоковані запити оновлення при перевстановленні, додано Python-залежність `python3-psycopg`, а `vertep status` тепер показує стан оновлення.

## ПРАВИЛЬНА НАЗВА: 0.0.0.49
- `vertep update` та періодичний перевірник оновлень тепер автентифікуються за внутрішнім ключем хоста замість застарілих облікових даних адміністратора bootstrap, тому створення облікового запису First Run більше не викликає відповіді HTTP 401.
- Шляхи bootstrap, оновлення та rollback видаляють лише застарілі контейнери-замінники з хеш-префіксом Docker Compose перед відтворенням, дозволяючи перерваним оновленням продовжуватися без конфліктів імен контейнерів, зберігаючи томи та конфігурацію.
- Привілейований агент оновлення використовує той самий внутрішній ключ для перевірок готовності drain під час оновлення.
- `vertep status` на стороні хоста тепер також використовує внутрішню автентифікацію, а підписані контракти runtime звітують генерацію схеми бази даних 9 після міграції розширення токенів вузлів.

## ПРАВИЛЬНА НАЗВА: 0.0.0.48
- Введено `INTERNAL_API_KEY` для ідентифікації внутрішніх запитів оновлення: Core middleware пропускає його для `/api/status` та `/api/system/update/*`, `update-agent` використовує ключ замість пароля, а `vertep-update-check.service` надсилає заголовок `X-Vertep-Internal-Key`; піднято версію `database_schema` до 9.

## ПРАВИЛЬНА НАЗВА: 0.0.0.47
- Відновлені інсталяції тепер розв'язують сервіси та можливості з завантаженого каталогу ролей, використовуючи його фактичну верхньорівневу схему; повторний bootstrap після First Run більше не завершується помилкою `Cannot iterate over null`.
- Bootstrap відхиляє збережену роль, яка відсутня в підписаному каталозі, з явною діагностикою та більше не пропонує непідтримувану застарілу роль `core-worker`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.46
- Спрощено Bootstrap: видалено роль `core-worker` з меню, перевірка ролі тепер використовує `has($role)` у `node_roles.json` і перейшла на `.[$role].services[]`, а тести перевіряють нову схему каталогу.

## ПРАВИЛЬНА НАЗВА: 0.0.0.45
- Додано міграцію бази даних лише вперед для прапорця `push_token` токена реєстрації вузлів, виправлено помилку завершення First Run як на існуючих, так і на нових інсталяціях.
- Токени реєстрації Core та інтеграційні секрети тепер готуються до того, як First Run буде незворотно позначено як завершене, запобігаючи випадку, коли помилка після коміту залишає майстра в частково завершеному стані.

## ПРАВИЛЬНА НАЗВА: 0.0.0.44
- Перенесено створення `registration_token` перед фіксацією First Run, щоб токен зберігався навіть при помилці; додано міграцію `push_token` для `node_registration_tokens` та відповідні тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.43
- First Run тепер відкладає валідацію та встановлення моделі для керованого пристроєм бекенду Ollama до моменту, коли розгортання ролі запускає Ollama; ролі Core і Text очікують сервіс, а потім завантажують обрану модель.
- Майстер завершує налаштування перед відкриттям кроку Installation Manifest, заповнює маніфест перед увімкненням кнопки завантаження та більше не стверджує, що незавершений маніфест вже доступний.

## ПРАВИЛЬНА НАЗВА: 0.0.0.42
- Оновлено валідацію AI backend: для локального Ollama перевірка контактування з сервером відкладається до deployment, First Run тепер зберігає `ai_backend` у deployment-запиті, а тести перевіряють обробку managed Ollama.

## ПРАВИЛЬНА НАЗВА: 0.0.0.41
- Майстер першого запуску більше не падає після успішної відповіді API налаштування: відсутні елементи керування підключенням Core присутні, рендеринг ролей терпить відсутність необов'язкового розмітки, та ручне введення коду налаштування оновлює URL замість безкінечного перезавантаження застарілого токена.

## ПРАВИЛЬНА НАЗВА: 0.0.0.40
- Додано перевірку у тесті, що елементи форми Core URL/Certificate/Token присутні у `setup.html`, відповідно оновлено сам wizard.

## ПРАВИЛЬНА НАЗВА: 0.0.0.39
- `/setup` тепер перенаправляє на реальний Майстер першого запуску за адресою `/setup.html`, зберігаючи одноразовий параметр запиту токена, замість повернення заповнювача неналаштованого runtime.

## ПРАВИЛЬНА НАЗВА: 0.0.0.38
- Додано route `/setup` з редиректом на `/setup.html` зі збереженням токена, оновлено `AdminAuthMiddleware` для обробки цього route, а також додано тест на переадресацію.

## ПРАВИЛЬНА НАЗВА: 0.0.0.37
- Healthcheck проксі тепер явно направляється на IPv4 loopback, уникаючи випадку, коли Alpine розв'язує `localhost` як неприв'язану IPv6-адресу та повідомляє про здоровий процес nginx як `unhealthy`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.36
- Замінено `localhost` на `127.0.0.1` у healthcheck проксі, оновлено тест деплоймент-плану для перевірки нового значення.

## ПРАВИЛЬНА НАЗВА: 0.0.0.35
- Compose тепер передає налаштований `WEB_DOMAIN` у контейнер проксі, дозволяючи автономній точці входу рендерити дійсне `server_name` nginx.
- Bootstrap повідомляє стан контейнера кожні 30 секунд під час очікування здоров'я runtime замість виглядання бездіяльним до тайм-ауту.

## ПРАВИЛЬНА НАЗВА: 0.0.0.34
- Винесено логіку запуску проксі в окремий `docker/proxy/entrypoint.sh`, оновлено `Dockerfile` і `docker-compose.yml`, додано перевірки у тести, а `qualify-release.py` тепер включає новий скрипт в артефакт.

## ПРАВИЛЬНА НАЗВА: 0.0.0.33
- Логіка запуску проксі перенесена з чутливого до лапок вбудованого Compose-команду в автономну точку входу образу; свіжі та відновлені інсталяції більше не потрапляють у цикл перезапуску з `unexpected end of file`.
- Образ проксі тепер рендерить налаштований `WEB_DOMAIN` з його шаблону перед запуском nginx та продовжує перезавантажувати nginx при зміні TLS-сертифіката або CRL вузлів.

## ПРАВИЛЬНА НАЗВА: 0.0.0.32
- Додано `WEB_DOMAIN` як змінну середовища для проксі, оновлено `proxy.conf.template` без запасного значення за замовчуванням, а також додано тести на нову поведінку проксі.

## ПРАВИЛЬНА НАЗВА: 0.0.0.31
- Bootstrap більше не викликає `docker compose wait` для вже завершеного one-shot контейнера `migrate`; код завершення міграції перевіряється безпосередньо через Docker inspect, тому успішний resume доходить до healthcheck і показу Setup URL.
- Startup Recovery unit отримав коректну секцію `[Install]`, тож `systemctl enable` більше не виводить попередження про static unit.

## ПРАВИЛЬНА НАЗВА: 0.0.0.30
- Видалено `docker compose wait migrate` з Bootstrap, додано `[Install]` секцію до `vertep-startup-recovery.service` для автозапуску при boot, оновлено тести.

## ПРАВИЛЬНА НАЗВА: 0.0.0.29
- Bootstrap став resumable та idempotent: повторний запуск зберігає паролі PostgreSQL/Redis, ключі, TLS/Node CA, роль, домен, довільні локальні параметри й Docker volumes, оновлюючи лише підписаний runtime та керовані release-параметри.
- Resume відмовляється генерувати нові credentials поверх наявних Docker volumes або encrypted secret store, якщо відповідний ключ втрачено, замість створення несумісного частково працездатного стану.
- Self-update підключено безпосередньо до GitHub Releases: release workflow публікує окремий підписаний update manifest, updater перевіряє підпис і SHA-256 пакета, а download allowlist обмежено репозиторієм `fylypovych/vertep`.
- Після успішного оновлення host-side executors і systemd units синхронізуються з активним підписаним release; CLI коректно визначає роль із `NODE_ROLE` і працює через локальний HTTPS proxy.

## ПРАВИЛЬНА НАЗВА: 0.0.0.28
- Додано побудову та підпис `update-manifest.json` у GitHub Actions, `bootstrap.sh` тепер ігнорує активні оновлення при встановленні, зберігає існуючі секрети при resume, підвищено безпеку bootstrap-скрипту, додано збирання runtime bundle та багато нових тестів.

## ПРАВИЛЬНА НАЗВА: 0.0.0.27
- License Manager healthcheck більше не намагається ініціалізувати зашифроване сховище у read-only каталозі під час чистої інсталяції.
- Startup Recovery вмикається для наступних завантажень системи, але більше не запускається паралельно з початковим Compose deployment.

## ПРАВИЛЬНА НАЗВА: 0.0.0.26
- Bootstrap тепер запускає `vertep-startup-recovery.service` з `enable`, а не `enable --now`, щоб уникнути гонки з початковим розгортанням; `license_service` більше не ініціалізує порожній secret store при healthcheck.

## ПРАВИЛЬНА НАЗВА: 0.0.0.25
- Виправлено перший запуск PostgreSQL: випадкові паролі тепер URL-безпечні, а Core і `migrate` використовують libpq DSN, тому символи Base64 більше не пошкоджують hostname або порт у `DATABASE_URL`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.24
- Замінено генерацію секретів з `openssl rand -base64` на `openssl rand -hex`, оновлено `DATABASE_URL` на libpq-формат з двома лапками, щоб уникнути помилок зі спецсимволами в паролях.

## ПРАВИЛЬНА НАЗВА: 0.0.0.23
- Публічне розгортання об'єднано в одному репозиторії `fylypovych/vertep`: Bootstrap отримує підписаний runtime із GitHub Releases, а digest-pinned образи — з GHCR; додано автоматичне підписування, публікацію та перевірку bundle.
- Правило послідовних назв комітів і релізів підтримує перенесення за основою 100: після `0.0.0.99` — `0.0.1.0`, після `0.0.99.99` — `0.1.0.0`.
- Тести ізольовано у тимчасових каталогах і синхронізовано з фоновими задачами, щоб Linux CI не звертався до `/data` та не успадковував незавершені задачі сусідніх тестів.
- Публікація основного інсталяційного релізу більше не блокується надвеликим експериментальним AMD/ROCm образом, який перевищує ліміт GitHub-hosted runner; підтримувані образи публікуються атомарно перед створенням Release.
- Вилучено зайву зміну видимості GHCR через несумісний API: образи, опубліковані з публічного репозиторію, перевіряються анонімним registry-запитом і не блокують створення GitHub Release.
- Додано PostgreSQL-backed координацію rolling update для всього кластера: глобальний fencing через advisory lock та epoch, детермінований порядок вузлів, явне просування canary, блокування dispatch під час update/rollback, автоматичний і ручний rollback до попередньої версії та відновлення після перерваного запуску.
- Міграції підтримують resumable backfill-модулі з durable checkpoint; backup/restore тепер охоплює конфігурацію, storage, PostgreSQL і Redis, перевіряє SHA-256 та безпечно відновлює AES-256-GCM snapshot без path traversal.
- Fleet readiness тепер формується з persisted Jobs і Workers та вимагає завершення drain усіма зареєстрованими вузлами; Worker обробляє окремі update і rollback requests та не приймає нові задачі під час зміни версії.
- Production Bootstrap встановлює й перевіряє NVIDIA Container Toolkit або AMD ROCm, використовує окремі signed Compose overlays для NVIDIA/AMD та зберігає правильний GPU runtime під час deployment, update, rollback, watchdog і startup recovery.
- License Manager, Dispatcher, Scheduler і Certificate Manager винесено в окремі health-checked runtime-сервіси; Core використовує окремі Dispatcher/Scheduler boundaries, а License Manager читає write-only license key із зашифрованого secret store.
- First Run перевіряє AI endpoint, HTTPS-вимоги, credentials і наявність вибраної моделі; локальну Ollama-модель за потреби можна встановити автоматично без shell-доступу.
- Installation Manifest доповнено фактичними Docker image ID/digest, станом контейнерів і health установлених модулів; deployment завершується лише після переходу всіх вибраних сервісів у healthy state.
- У Web UI додано Zero-Shell lifecycle для створення та відновлення backup, встановлення й видалення Ollama models, перегляду та оновлення TLS certificate, а також API для отримання актуального Installation Manifest.
- Runtime-конфігурацію Proxy, Prometheus, Loki, Promtail і Grafana вбудовано в незмінні образи; mutable bind-mounted configuration overlays вилучено з production Compose.
- Виправлено пошкоджене злиття, яке дублювало перевірки безпеки, логіку оновлення, кроки bootstrap, конфігурацію розгортання, секції Web UI та тести; репозиторій знову компілюється, а посилений захист гілки збережено.
- Узгоджено `VERSION`, записи журналу змін і примітки до релізів з опублікованими комітами `0.0.0.3` та `0.0.0.5`.
- Нумерація релізів тепер враховує метадані релізів без тегів, тому наступний номер залишається монотонним; для локальної розробки й тестів додано сумісну з Windows оренду блокування оновлення.
- Стабілізовано тестовий baseline: ізольовано стан API та черги між тестами, актуалізовано heartbeat-контракт Worker, виправлено фонову гонку й додано коректну поведінку Linux-only перевірок у Windows.
- Додано підписаний контракт runtime-релізу версії 2, який зв'язує каталог ролей, файловий inventory, CycloneDX SBOM, сумісність API/бази даних і незмінні digest контейнерних образів; Bootstrap тепер передає Compose лише перевірені образи.
- Update Agent тепер записує глобальний стан у переданий йому каталог операції, не покладаючись на неявний системний шлях; це усуває розбіжність між журналом агента та станом Core у тестах і нестандартних інсталяціях.
- Linux CI тепер перевіряє appliance Compose з тим самим розташуванням `.env`, яке використовує встановлений runtime.
- Додано окремі TTS, Publisher і Backup runtime-сервіси та непривілейовані контейнерні образи: TTS повертає справжній WAV через `espeak-ng`, Publisher забезпечує ідемпотентні receipts без удаваного live-успіху, а Backup створює AES-256-GCM-зашифровані snapshot із SHA-256.
- Monitoring Node отримав Prometheus rules, Loki, Promtail, захищену Grafana з автоматично налаштованими джерелами даних і початковим dashboard журналів та стану runtime.
- Bootstrap став незалежним від майбутньої ролі вузла: він запускає лише тимчасовий контур налаштування, Web Wizard формує перевірюваний запит, а привілейований host-executor застосовує виключно сервіси з підписаного каталогу ролей і прибирає тимчасовий Core для non-Core вузлів.

## ПРАВИЛЬНА НАЗВА: 0.0.0.22
- Видалено зайві 8 рядків з `.github/workflows/release.yml` (очищення workflow).

## ПРАВИЛЬНА НАЗВА: 0.0.0.21
- Незначні правки `.github/workflows/release.yml`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.20
- Оновлено `VERSION` з `0.0.0.1` на `0.0.0.20` (проміжний bump без зміни коду).

## ПРАВИЛЬНА НАЗВА: 0.0.0.19
- Розширено `tests/conftest.py` додатковими фікстурами/утилітами для тестування.

## ПРАВИЛЬНА НАЗВА: 0.0.0.18
- Розбито GitHub Actions release workflow на окремі jobs `prepare`/`build-images`/`publish-release`, додано паралельну збірку образів для 14 сервісів з архітектурними платформами.
- Додано `scripts/assemble-image-lock.py` для immutable image metadata.
- Оновлено `bootstrap.sh` для роботи з новим сервером оновлень.
- Оновлено `installer/update-public.pem` для нової release-ceremony.

## ПРАВИЛЬНА НАЗВА: 0.0.0.17
- Створено Dockerfile для кожного сервісу: `docker/comfyui-amd/Dockerfile`, `docker/comfyui-nvidia/Dockerfile`, `docker/ollama/Dockerfile`, `docker/postgres/Dockerfile`, `docker/redis/Dockerfile` (через існуючі docker/).
- Додано `scripts/build-runtime-bundle.py` для збирання підписаного runtime bundle.
- Додано `services/update_agent_service.py`.
- Оновлено `deploy/docker-compose.yml` з командою для worker та healthcheck для update-agent.

## ПРАВИЛЬНА НАЗВА: 0.0.0.16
- Незначні правки `.github/workflows/release.yml` (6 рядків).

## ПРАВИЛЬНА НАЗВА: 0.0.0.15
- Додано `scripts/release-bundle.py` для створення runtime bundle.
- Оновлено GitHub Actions для автоматичної збірки образів, пушу в GHCR та створення GitHub Release з артефактами.
- Змінено `DOWNLOAD_ORIGIN` на GitHub Releases API.
- Суттєво оновлено `bootstrap.sh` для підтримки нових release-процесів.

## ПРАВИЛЬНА НАЗВА: 0.0.0.14
- Оновлено `README.md`: перейменовано розділ «Ubuntu installation» на «Legacy/source installation», додано опис production installation через bootstrap з посиланням на download.vertep.ai.

## ПРАВИЛЬНА НАЗВА: 0.0.0.13
- Оновлено посилання на bootstrap у `README.md` та `web/index.html` з `download.vertep.ai` на `raw.githubusercontent.com`.

## ПРАВИЛЬНА НАЗВА: 0.0.0.12
- Значно перероблено `bootstrap.sh`: додано перевірки, генерацію секретів, підтримку ролей, завантаження runtime, перевірку підпису, міграції БД, systemd-юніти та завершення First Run Wizard (78+ / 59-).

## ПРАВИЛЬНА НАЗВА: 0.0.0.11
- Зроблено виконуваними інсталяційні скрипти: `installer/detect-gpu.sh`, `installer/install-comfyui.sh`, `installer/preflight.sh`, `installer/roles/backup.sh`, `installer/roles/monitoring.sh`, `installer/roles/publisher.sh`, `installer/roles/text.sh`, `installer/roles/voice.sh` (chmod 755).

## ПРАВИЛЬНА НАЗВА: 0.0.0.10
- Зроблено виконуваним CLI-скрипт `scripts/vertep` (chmod 755).

## ПРАВИЛЬНА НАЗВА: 0.0.0.9
- Зроблено виконуваним скрипт `install.sh` (chmod 755).

## ПРАВИЛЬНА НАЗВА: 0.0.0.8
- Розширено API: додано ендпоінти для керування текстовими моделями Ollama (`/api/models/text/*`) та голосовими моделями (`/api/models/voices`).
- Об'єднано інформацію про worker з реєстром вузлів.
- Додано дію `rotate` для ротації сертифікатів.
- Оновлено publisher service до v2 з розширеними можливостями публікації.
- Оновлено `worker/role_executor.py` для підтримки нових типів задач.

## ПРАВИЛЬНА НАЗВА: 0.0.0.7
- Додано скоординовані поступові оновлення з лизами обслуговування, відкачуванням навантаження, тривалими фазами, перевірками здоров'я та автоматичним rollback.
- Додано threshold-signed root metadata, авторизацію release-key, захист від повторного відтворення та примусове відкликання сертифікатів вузлів на проксі.
- Додано незмінне підготовлення релізу, атомарну активацію, rollback та інструменти збереження.
- Додано зашифровані інтеграційні секрети тільки для запису та посилений авторизацію оновлення секретів та ізоляцію ролей.
- Додано виконання ролей воркера з обмеженням можливостей та розширено enrollment вузлів, ротацію сертифікатів та поведінку самотестування.
- Додано репродуковані ворота кваліфікації пристрою та генерацію CI-доказів.
- Посилено перевірку хоста bootstrap, залежності runtime, ізоляцію служби оновлювача та відновлення після перерваних оновлень.
- Масштабно оновлено `bootstrap.sh`, `core/app.py`, `core/first_run.py`, `core/health_checks.py` та конфігурацію `config/node_roles.json` для підтримки нових ролей і обов'язкових capabilities.

## ПРАВИЛЬНА НАЗВА: 0.0.0.6
- Додано підтримку rolling update з canary deployment: нові endpoint `/api/system/update/rolling/cancel` та `/api/system/update/rolling/rollback`, поле `canary` у моделі запиту, автоматичний rollback при помилці canary.
- Додано окремий сервіс `backup-service` у `deploy/docker-compose.yml` та його реалізацію в `services/backup_service.py`.
- Оновлено `core/rolling_update.py` для координації canary з advisory lock PostgreSQL.

## ПРАВИЛЬНА НАЗВА: 0.0.0.5
- Незначні правки `tests/test_browser_e2e.py` (2 рядки).

## ПРАВИЛЬНА НАЗВА: 0.0.0.4
- Створено GitHub Actions workflow для Browser E2E-тестів з Playwright.
- Додано документацію церемонії випуску реліз-ключа `docs/RELEASE_KEY_CEREMONY.md`.
- Додано скрипт `scripts/generate-root-metadata.py` для генерації кореневих метаданих.
- Додано скрипт `scripts/production-acceptance.sh` для production-перевірок.
- Додано початковий `tests/test_browser_e2e.py` з базовим покриттям.

## ПРАВИЛЬНА НАЗВА: 0.0.0.3
- Додано потоковий сценарій bootstrap та Майстер першого запуску з семи кроків.
- Додано підписані оновлення з перевіркою контрольної суми, періодичні перевірки релізів, резервні копії, перевірки здоров'я та підтримку rollback.
- Додано реєстр вузлів, одноразові токени enrollment воркерів, облікові дані, прив'язані до вузла, атестацію сертифікатів та диспетчеризацію на основі можливостей.
- Додано розширювані ролі вузлів та планування розгортання з `config/node_roles.json`.
- Додано продуктивний проксі та топологію Compose з постійними сервісами та завершенням TLS.
- Додано міграції бази даних для реєстру вузлів та життєвого циклу сертифікатів.
- Додано самотестування обладнання воркера та автоматичну ротацію термінових сертифікатів вузлів.
- Додано документацію з безпеки розгортання та вимог інтеграції з покриттям.

## ПРАВИЛЬНА НАЗВА: 0.0.0.2
- Додано підтримку `push_token` у створенні токена реєстрації: новий параметр `push_token` у `create_registration_token`, оновлено SQL-запит вставки та JSON-серіалізацію відповіді для PostgreSQL і файлового сховища.

## ПРАВИЛЬНА НАЗВА: 0.0.0.1
- Додано стан DAG сцени/сцен, історію спроб та диспетчеризацію fan-out/fan-in для кожної сцени.
- Додано повторні спроби для кожної сцени, чергу dead-letter, відкладені Job розклади та відновлення завдань воркера.
- Додано маніфести артефактів SHA-256, provenance, перевірки цілісності та перевірені завантаження.
- Додано завантаження без залежностей та портативний імпорт/експорт ZIP-проєктів.
- Додано TTS для кожної сцени, точний тайминг сцен та конкатенацію нарації.
- Додано нормалізоване збереження PostgreSQL для сцен, артефактів та stage-attempt.
- Додано керування таймлайном, завантаженнями, експортом і dead-letter в Web UI та прогрес сцени в Telegram.
- Додано перевірки оптимістичних версій Job та розширене інтеграційне покриття.
- Додано відновлення сцен без падіння, повторні спроби сценарію/TTS/publisher та перевірені метадані платформи.
- Додано диспетчеризацію з обізнаністю про можливості та видалено head-of-line blocking відкладених завдань.
- Відтворено Web UI як чистий UTF-8 український розмітку з виглядами черги, планувальника та оркестрації.
- Додано автономний стек WORKER Compose, який не може запускати CORE, PostgreSQL або Redis.
- Зроблено оновлення, rollback і статус з обізнаністю про ролі, включаючи токен статусу вузла та обробку offline CORE.
- Додано пакети інсталятора, керовані маніфестом, та розширено попередні перевірки лише для читання.
- Ізольовано ComfyUI хоста на loopback та додано захист SSH-ключа без блокування та політику UFW за замовчуванням deny.
- Змінено оновлення CORE на очікування PostgreSQL і застосування міграцій перед перебудовою додатку.
- Прив'язано кожен результат завдання до воркера, якому належить lease, та скасовано сістерські завдання після фатальної помилки сцени.
- Додано перевірку бінарного підпису, атомарне отримання артефактів та серіалізовану обробку результатів за Job.
- Зроблено так, щоб воркери перетворювали відхилені згенеровані артефакти в нормальні невдалі результати для обробки retry/DLQ.
- Зареєстровано вкладення Telegram в маніфест артефактів та примусово перевірені завантаження на застарілих маршрутах файлів.
- Додано першокласні розподілені відео артефакти та конкатенацію FFmpeg кліпів сцен.
- Додано явний ERROR heartbeat воркера, коли потрібний GPU недоступний.
- Додано Web-елементи керування для видалення, повторної публікації, брендів та workflow.
- Зроблено ролі/профіль композиції інсталятора розширюваними через маніфест та необов'язкові хуки ролей.
- Додано GitHub Actions перевірки для Python, тестів, синтаксису shell та конфігурацій Compose.
- Додано профіль воркера GTX 1660 6 GB/Turing з закріпленими колесами PyTorch CUDA 12.4, режимом ComfyUI low-VRAM, перевіркою CUDA та метаданими heartbeat.
- Додано перевірки оновлень GitHub тільки для адміністраторів та встановлення з Web UI через постійний systemd агент хоста без відкриття Docker сокета.
