# План повної реалізації Web UI V2 Vertep

## 1. Мета

Довести Web UI V2 до затвердженої концепції Zero Shell: усі штатні операції адміністратора, керування Job, Worker, Publisher, оновленнями, відновленням і monitoring виконуються через український Web UI.

План базується на аудиті поточного frontend, FastAPI routes, runtime services, adapters, installer та monitoring. Mock, stub, placeholder і read-only відображення не вважаються завершеною реалізацією керування.

## 2. Правила виконання для агентів

- Перед початком прочитати `AGENTS.md` і цей файл.
- Взяти один task ID або один явно узгоджений набір залежних task ID.
- Не змінювати файли, закріплені за іншим активним task.
- Спочатку зафіксувати API contract і state transitions, потім реалізовувати UI.
- Не додавати `skip`, `xfail`, timeout, штучний `setTimeout`, reload або hardcoded success state.
- Для асинхронного UI state у zoneless Angular використовувати `signal()`/`computed()` або інший механізм, який гарантовано планує render.
- Не створювати паралельний backend route, якщо реальний route уже визначений у `core/app.py` або `core/api/`.
- Не використовувати stub-модуль `core/api/system.py` як джерело production-поведінки: фактичні system/update routes зараз визначені у `core/app.py`.
- Після task оновити таблицю прогресу в розділі 12 і додати короткий запис до журналу рішень.
- Не виконувати commit, push або release без окремої команди користувача.

## 3. Цільова структура Web UI V2

| Route | Призначення |
|---|---|
| `/` | Dashboard: KPI, system state, resources, alerts, fleet overview |
| `/jobs` | Список, фільтри, створення і масові операції Job |
| `/jobs/:id` | Job Detail: lifecycle actions, editor, stages, timeline, artifacts, publication |
| `/queue` | Ready, scheduled, inflight і dead-letter task |
| `/published` | Опубліковані матеріали, platform receipts і retry |
| `/workers` | Fleet, metrics, registration, control і credentials state |
| `/workers/:id` | Деталі Worker, hardware, capabilities, self-test, logs, update state |
| `/characters` | Повний редактор character-конфігурації |
| `/workflows` | Registry, editor, validation і references |
| `/publishing` | Brands, channels, Publisher readiness і test publish |
| `/telegram` | Bot setup, transport mode, chats і стан ingestion |
| `/operations` | Alerts, logs, health history і monitoring |
| `/settings` | Загальні налаштування, providers, models, logo |
| `/settings/secrets` | Masked Secrets Store |
| `/settings/roles` | Node Roles і локальний deployment plan |
| `/settings/update` | Update, readiness, rolling update і rollback |
| `/settings/backups` | Snapshots, restore і retention |
| `/setup` | First Run Wizard V2 |

## 4. Базові технічні рішення

### 4.1 API client

Розділити поточний `VertepApiService` на доменні clients або чіткі секції:

- `JobsApi`
- `WorkersApi`
- `QueueApi`
- `ResourcesApi`
- `PublishingApi`
- `OperationsApi`
- `SystemApi`
- `SetupApi`

Усі request/response типи мають бути описані TypeScript interfaces. Заборонено використовувати `any` для нових contracts.

### 4.2 UI state

- Remote data: `signal()` із явними `loading`, `error`, `data`.
- Derived data: `computed()`.
- Кожна mutation має стани idle/running/succeeded/failed.
- API errors показуються українською, але технічний detail зберігається для diagnostics.
- Після mutation оновлювати лише залежні дані, без повного reload сторінки.

### 4.3 Навігація та permissions

- Sidebar показує всі production-розділи.
- Viewer бачить read-only UI без mutation controls.
- Admin має mutation controls.
- Недоступна через system state дія має бути disabled із поясненням причини.

### 4.4 Тести

Для кожного етапу:

- unit/contract tests для нового mapping або state transition;
- Browser E2E для основного happy path і критичної помилки;
- відсутність JavaScript `pageerror`;
- `python -m compileall -q core adapters worker scripts installer tests`;
- `python -m pytest -q`;
- `python -m pytest tests/test_browser_e2e.py -q`;
- `npm run build` у `web-v2`;
- `git diff --check`.

## 5. Етап 0 — стабілізація контрактів

### V2-001. Інвентаризація і typed API contracts

Залежності: немає.

Зміни:

- Описати TypeScript-моделі Job, Scene, Stage, Artifact, Worker, Node, QueueTask, Channel, Alert, Update і SecretStatus.
- Усунути дублікати та `any` у `web-v2/src/app/core/api.service.ts`.
- Зафіксувати фактичні endpoints із `core/api/` і `core/app.py`.

Готово, коли frontend compile-time перевіряє всі використовувані API fields.

### V2-002. Виправлення відомих contract defects

Залежності: V2-001.

Зміни:

- `scheduled_at` → `scheduled_for` у create Job.
- Або додати `topic` до `JobUpdate`, або прибрати його з editor; цільове рішення — підтримати редагування topic з optimistic version check.
- Worker state `READY` вважати доступним; не рахувати лише `ONLINE`.
- Mapping metrics: `gpu_load`, `cpu_load`, `temperature`, `vram_mb`, `free_vram_mb`.
- Прибрати frontend calls до неіснуючих `POST /api/nodes` і `DELETE /api/nodes/{id}`.

Готово, коли contract tests підтверджують точний payload і response mapping.

### V2-003. Спільні UI primitives

Залежності: V2-001.

Створити reusable компоненти: loading/error/empty state, status badge, action toolbar, confirmation, JSON technical details, paginated/filterable table, mutation progress.

## 6. Етап 1 — основний Job lifecycle

### V2-101. Повна форма створення Job

Залежності: V2-002, V2-003.

Поля: topic, character, priority, task type, workflow, brand, aspect ratio, output preset, scheduled_for. Дані selector завантажувати з `/api/characters`, `/api/workflows`, `/api/brands`.

### V2-102. Lifecycle actions у Job Detail

Залежності: V2-001, V2-003.

Підключити:

- `POST /api/jobs/{id}/pause`
- `POST /api/jobs/{id}/resume`
- `POST /api/jobs/{id}/retry`
- `POST /api/jobs/{id}/regenerate`
- `POST /api/jobs/{id}/cancel`
- `POST /api/jobs/{id}/approve`
- `DELETE /api/jobs/{id}`

Кнопки показувати лише у допустимих станах. Regenerate має пояснювати, які похідні artifacts буде видалено.

### V2-103. Job editor

Залежності: V2-002, V2-101.

Редагування: topic, character, priority, workflow, script і prompts сцен. Передавати `expected_version`; HTTP 409 показувати як conflict із можливістю перечитати актуальний Job.

### V2-104. Stages, timeline та errors

Залежності: V2-001.

Побудувати timeline зі `stages`, `scenes`, `events`, attempts, worker assignment і errors. Події мають timestamps; якщо backend їх не надає структуровано, додати backward-compatible structured event model.

### V2-105. Artifacts workspace

Залежності: V2-001.

Підключити list, integrity verify, download, reference/audio upload, preview, project export/import. Не показувати artifact як валідний без результату integrity check.

### V2-106. Publish flow

Залежності: V2-102, V2-301.

Додати вибір channels, approve-before-publish policy, progress, `publication_results`, platform URLs і retry failed publication.

## 7. Етап 2 — Queue, Published і Operations

### V2-201. Queue page

Залежності: V2-001, V2-003.

Показати scheduled, ready, inflight і dead-letter task. Для dead-letter підключити `/api/tasks/dead-letter` і retry. Для ready/inflight за потреби додати read-only backend endpoint без доступу до внутрішніх Redis деталей.

### V2-202. Published page

Залежності: V2-106.

Додати backend query/filter contract для `PUBLISHED` Job, таблицю publication receipts, platform URLs, channel filters і retry failed publication.

### V2-203. Alerts

Залежності: V2-003.

Підключити `/api/alerts`. Додати severity, source, Job/Worker links і refresh. Якщо потрібен acknowledgment, спочатку реалізувати persistent backend lifecycle alerts.

### V2-204. Logs

Залежності: V2-003.

Підключити `/api/logs` із filters `level`, `job_id`, `node_name`, limit. Реалізувати polling або SSE без блокування UI.

### V2-205. Health і metrics

Залежності: V2-003.

Відображати `/api/health`, `/api/health/history`, `/api/metrics`; додати посилання на Grafana. Не дублювати повний Grafana всередині Angular без потреби.

## 8. Етап 3 — Workers, Roles і Capabilities

### V2-301. Worker onboarding

Залежності: V2-001, V2-003.

Замість локального «створити worker» реалізувати:

1. вибір role;
2. `POST /api/nodes/registration-tokens`;
3. показ одноразового token, TTL, Core URL і certificate;
4. очікування появи вузла в `/api/nodes`;
5. результат registration/self-test.

### V2-302. Worker detail і metrics

Залежності: V2-002, V2-301.

Показати hardware, GPU, VRAM total/free, temperature, GPU/CPU load, RAM/disk, heartbeat age, current Job/task, supported workflows, capabilities, runtime version, certificate expiry і self-test.

### V2-303. Worker controls

Залежності: V2-302.

Підключити drain, resume, quarantine, unquarantine, self-test, disable, enable, restart і revoke. Revoke замінює неіснуючий delete. Для `logs` або реалізувати backend response, або вести до Operations із `node_name` filter.

### V2-304. Roles and Capabilities settings

Залежності: V2-003.

Підключити GET/POST `/api/system/roles`, показувати active/requested roles, services, capabilities і deployment status. Зміни ролей мають показувати progress системного executor та health result.

## 9. Етап 4 — Characters, Workflows і Publishing

### V2-401. Повний Character editor

Залежності: V2-003.

Редагувати всі конфігураційні області: base metadata, `system_prompt`, voice, visual, generation, workflow, retries, publishing. ID генерує система; після створення він immutable.

### V2-402. Workflow registry/editor

Залежності: V2-001, V2-003.

Підключити CRUD `/api/workflows`, schema validation, JSON editor/form view, reference usage та безпечне видалення.

### V2-403. Brands

Залежності: V2-003.

Підключити CRUD brands. Backend не має окремого create route; або додати `POST /api/brands`, або документувати idempotent creation через узгоджений PUT contract.

### V2-404. Channels і Publisher settings

Залежності: V2-403, V2-501.

Підключити channel types і CRUD channels. Форми мають враховувати platform metadata, target, enabled state, credential readiness і test publish.

### V2-405. Telegram settings

Залежності: V2-501.

Підключити setup/status/bot-info, webhook або polling mode, allowed/admin chat IDs. Завершити backend `/character` command перед позначенням функції готовою.

## 10. Етап 5 — Settings, Secrets, Update і Recovery

### V2-501. Secrets Store UI

Залежності: V2-001, V2-003.

Підключити GET/PUT/DELETE `/api/settings/secrets`. Значення ніколи не читати назад; показувати лише configured/missing. Додати rotate/delete confirmation і platform grouping.

### V2-502. Providers і Models

Залежності: V2-501.

Розширити read-only provider matrix керуванням дозволеними settings. Додати model list/pull/delete, pull progress і voice synthesis preview. Активний backend змінювати через офіційний deployment/configuration flow.

### V2-503. Update Center

Залежності: V2-203, V2-304.

Підключити status, check, readiness, install, restart, rolling status/start/cancel, canary promote/rollback. Показувати system state, drain acknowledgments, active Job, busy/unacknowledged workers і update log.

### V2-504. Backup і Recovery

Залежності: V2-503.

Підключити snapshot list/create/restore, recovery-to-NORMAL і update rollback. Кожна destructive операція потребує concrete target, health state та confirmation.

### V2-505. General settings

Залежності: V2-501.

Додати integrations health, logo upload/delete, installation manifest, certificate status/renewal і security check.

## 11. Етап 6 — First Run Wizard V2 і завершення Zero Shell

### V2-601. Angular First Run Wizard

Залежності: V2-001, V2-003, V2-301, V2-501.

Перенести `web/setup.html` у standalone Angular route `/setup`. Зберегти setup-token protection. Реалізувати кроки role, installation, admin, hardware, AI backend, health check, manifest і done.

### V2-602. Non-Core enrollment

Залежності: V2-601.

Поля Core URL, Core Certificate, Registration Token; показ CSR/enrollment errors, credential persistence і появу вузла у Core.

### V2-603. Production UX hardening

Залежності: усі попередні етапи.

- role-based visibility;
- system-state action guards;
- responsive/mobile layout;
- accessibility і keyboard navigation;
- consistent Ukrainian terminology;
- empty/error/loading states;
- route-level lazy loading;
- removal of legacy V1 navigation after feature parity.

### V2-604. Acceptance suite

Залежності: V2-603.

Побудувати Browser E2E сценарії для повного admin flow: setup → worker onboarding → character/workflow → Job → approval → publish → update readiness → backup/recovery. Production acceptance не використовує mock Publisher як доказ live publication.

## 12. Таблиця прогресу

Статуси task: `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`.

| Task | Статус | Залежності | Основні файли/область |
|---|---|---|---|
| V2-001 Typed API contracts | DONE | — | `web-v2/src/app/core/` |
| V2-002 Contract defects | DONE | V2-001 | Jobs, Workers, API models |
| V2-003 Shared UI primitives | DONE | V2-001 | `web-v2/src/app/shared/` |
| V2-101 Create Job | DONE | V2-002, V2-003 | Jobs UI/API |
| V2-102 Job lifecycle actions | DONE | V2-001, V2-003 | Job Detail/API |
| V2-103 Job editor | DONE | V2-002, V2-101 | Job Detail, `JobUpdate` |
| V2-104 Timeline/errors | DONE | V2-001 | Job Detail, event model |
| V2-105 Artifacts | DONE | V2-001 | Job Detail/artifact API |
| V2-106 Publish flow | DONE | V2-102, V2-301 | Job Detail/Publisher |
| V2-201 Queue | DONE | V2-001, V2-003 | Queue/task API |
| V2-202 Published | DONE | V2-106 | Published UI/query API |
| V2-203 Alerts | DONE | V2-003 | Operations/alerts API |
| V2-204 Logs | DONE | V2-003 | Operations/logs API |
| V2-205 Health/metrics | DONE | V2-003 | Operations/observability |
| V2-301 Worker onboarding | DONE | V2-001, V2-003 | Workers/nodes API |
| V2-302 Worker detail | DONE | V2-002, V2-301 | Workers/heartbeat model |
| V2-303 Worker controls | DONE | V2-302 | Workers/node actions |
| V2-304 Roles/capabilities | DONE | V2-003 | Settings/system roles |
| V2-401 Character editor | DONE | V2-003 | Characters/resources API |
| V2-402 Workflow editor | DONE | V2-001, V2-003 | Workflows UI/API |
| V2-403 Brands | DONE | V2-003 | Publishing/resources API |
| V2-404 Channels | DONE | V2-403, V2-501 | Publishing/channels API |
| V2-405 Telegram | DONE | V2-501 | Telegram UI/API |
| V2-501 Secrets Store | TODO | V2-001, V2-003 | Settings/secrets API |
| V2-502 Providers/models | TODO | V2-501 | Settings/models API |
| V2-503 Update Center | TODO | V2-203, V2-304 | Settings/update API |
| V2-504 Backup/recovery | TODO | V2-503 | Settings/system API |
| V2-505 General settings | TODO | V2-501 | Settings/integrations |
| V2-601 Setup Wizard V2 | TODO | V2-001, V2-003, V2-301, V2-501 | Setup Angular/API |
| V2-602 Non-Core enrollment | TODO | V2-601 | Setup/nodes/PKI |
| V2-603 UX hardening | TODO | усі feature tasks | весь `web-v2` |
| V2-604 Acceptance suite | TODO | V2-603 | browser E2E/acceptance |

## 13. Рекомендований порядок delivery

1. V2-001, V2-002, V2-003.
2. V2-101, V2-102, V2-103, V2-104, V2-105.
3. V2-201, V2-203, V2-204, V2-205.
4. V2-301, V2-302, V2-303, V2-304.
5. V2-501, V2-401, V2-402, V2-403, V2-404, V2-405.
6. V2-106, V2-202.
7. V2-502, V2-503, V2-504, V2-505.
8. V2-601, V2-602, V2-603, V2-604.

Перший production milestone: повний Job lifecycle плюс Queue і Worker onboarding. Другий: Publishing та Operations. Третій: Update/Recovery і First Run Wizard V2. Після третього milestone можна прибирати залежність адміністратора від V1 UI та shell.

## 14. Definition of Done для всього плану

- Усі task у таблиці мають статус `DONE`.
- Усі штатні admin operations доступні у V2 без shell.
- Sidebar не містить dead links і placeholder pages.
- Кожна UI mutation використовує реальний backend route та перевірений contract.
- V2 не викликає неіснуючих endpoints.
- Відсутні mock/stub як production success path.
- First Run Wizard працює у V2 для Core і non-Core.
- Job можна створити, відредагувати, провести через lifecycle, approve і publish.
- Worker можна зареєструвати, перевірити, drain/resume, оновити та revoke.
- Update Center виконує readiness, drain, backup, install, health check, rolling update і rollback.
- Logs, alerts, metrics і recovery доступні через V2.
- Повний test suite і production acceptance проходять без skip, timeout workaround та JavaScript errors.

## 15. Журнал рішень

| Дата | Task | Рішення | Причина |
|---|---|---|---|
| 2026-09-07 | PLAN | Створено початковий план Web UI V2 | За результатами аудиту concept ↔ backend ↔ V2 |
| 2026-09-07 | V2-001 | Typed API-контракти синхронізовано з backend | Інвентаризація `core/api/` + `core/app.py`, заміна `any` на interfaces, додано 20+ моделей |
| 2026-09-07 | V2-002 | Виправлено contract defects | Додано `topic` у `JobUpdate`, `scheduled_for`, READY/FREE worker status, metrics mapping, видалено неіснуючі nodes endpoints |
| 2026-09-07 | V2-003 | Створено спільні UI primitives | loading/error/empty state, status badge, action toolbar, confirmation, JSON viewer, paginated table, mutation progress |
| 2026-09-07 | V2-101 | Повна форма створення Job | Додано всі поля: character, task type, workflow, brand, aspect ratio, output preset, scheduled_for; підключено селектори з API |
| 2026-09-07 | V2-102 | Lifecycle actions у Job Detail | Підключено pause/resume/retry/regenerate/cancel/approve/delete; guards за станами; regenerate warning; mutation progress |
| 2026-09-07 | V2-103 | Job editor з optimistic version check | Редагування topic/character/workflow/script; 409 conflict UI з перезавантаженням; селектори characters/workflows |
| 2026-09-07 | V2-104 | Timeline, stages, scenes, errors | Додано timeline з stages/scenes/events; worker assignment, attempts, errors; backward-compatible structured events |
| 2026-09-07 | V2-105 | Artifacts workspace | Integrity verify, download, upload references/audio, export/import; валідність показується тільки після перевірки |
| 2026-09-07 | V2-301 | Worker onboarding | registration-tokens flow: role selection, token/TTL/Core URL, polling /api/nodes, registration result |
| 2026-09-07 | V2-302 | Worker detail page | GET /api/nodes/{node_id} endpoint; hardware, GPU, VRAM, temp, load, heartbeat age, capabilities, cert expiry, self-test |
| 2026-09-07 | V2-303 | Worker controls | drain/resume/quarantine/unquarantine/self-test/disable/enable/restart/logs/update/revoke у worker detail |
| 2026-09-07 | V2-304 | Roles/capabilities settings | GET/POST /api/system/roles; active/available roles, services, capabilities, deployment status |
| 2026-09-07 | V2-401 | Character editor (full config) | Expanded form: base metadata, system_prompt, voice/visual/generation/publishing JSON editors; getCharacter endpoint |
| 2026-09-07 | V2-402 | Workflow registry/editor | GET/PUT/DELETE /api/workflows/{kind}/{name}; JSON editor с validation, usage reference |
| 2026-09-07 | V2-403 | Brands CRUD | POST/GET/PUT/DELETE /api/brands; createBrand endpoint додано у backend |
| 2026-09-07 | V2-404 | Channels management | Inline channel CRUD у brands; channel types, target, enabled, credential readiness |
| 2026-09-07 | V2-405 | Telegram settings | Status, bot-info, webhook/polling mode, allowed/admin chat IDs у Settings
