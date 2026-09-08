# План завершення Web UI V2 Vertep

## 1. Призначення і baseline

Цей документ замінює таблицю прогресу з `docs/WEB_UI_V2_IMPLEMENTATION_PLAN_UK.md` як актуальне джерело стану робіт. Початковий документ залишається описом концепції та історії першого етапу.

Baseline аудиту:

- commit: `3255a7a5a66d69f5aa22f19bd0fd6504c684bd3a` (`0.0.1.21`);
- дата аудиту: 2026-09-08;
- фактичний стан тестів після локального виправлення імпорту Logs: `1 failed, 270 passed, 9 skipped`;
- Angular build локального робочого дерева проходить;
- Angular build самого commit `3255a7a5` падає через відсутній `web-v2/src/app/logs/logs.component.ts`;
- незакомічені виправлення Logs не вважаються частиною baseline commit.

Мета: виконати всі вимоги початкового плану та довести Web UI V2 до Zero Shell стану, у якому штатні адміністративні операції доступні через український UI, використовують реальні backend contracts і підтверджені Browser E2E.

## 2. Правила роботи агентів

1. Перед роботою прочитати `AGENTS.md`, початковий план і цей документ.
2. Взяти один task ID або явно пов'язаний набір залежних задач.
3. Перед змінами перевірити `git status`; не перезаписувати чужі незакомічені зміни.
4. Статус `DONE` дозволений лише після виконання всіх acceptance criteria задачі.
5. Наявність компонента, кнопки чи API method сама по собі не означає завершення функції.
6. Mock, stub, `pass`, placeholder і hardcoded response не є production implementation.
7. Не використовувати `skip`, `xfail`, збільшення timeout, `setTimeout`, manual reload або послаблення assertion.
8. Для zoneless Angular асинхронний state зберігати через `signal()`/`computed()`.
9. Mutation controls мають враховувати роль користувача і system state.
10. Не виконувати commit, push або release без окремої команди користувача.
11. Після завершення задачі оновити таблицю прогресу і журнал рішень цього документа.

## 3. Definition of Done

Задача отримує `DONE`, коли одночасно виконано:

- backend route має production implementation;
- TypeScript request/response contract типізований без `any` у межах функції;
- UI має loading, empty, error і success states;
- mutation має running/failed/succeeded state та безпечне підтвердження, коли потрібне;
- permissions і system-state guards застосовані;
- додано змістовний API/contract test;
- додано Browser E2E для основного сценарію та критичної помилки;
- немає JavaScript `pageerror`;
- Angular build і релевантні pytest проходять.

Весь план завершено лише після:

```text
python -m compileall -q core adapters worker scripts installer tests
python -m pytest -q
python -m pytest tests/test_browser_e2e.py -q
npm run build
git diff --check
```

Повний pytest має пройти без нових або замаскованих skip. Усі production acceptance сценарії мають виконуватися без mock Publisher як доказу live publication.

## 4. Етап A — повернення зеленого baseline

### V2C-001. Включити Logs component у source tree

Файли: `.gitignore`, `web-v2/src/app/logs/logs.component.ts`, `web-v2/src/app/app.routes.ts`, `web-v2/src/app/core/models.ts`.

Роботи:

- обмежити ignore rule для runtime-логів кореневою директорією `/logs/`;
- додати production `LogsComponent` у Git;
- підключити `GET /api/logs` з filters `level`, `job_id`, `node_name`, `limit`;
- додати контрольований polling або SSE з коректним cleanup;
- перевірити loading/error/empty/table states;
- додати Browser E2E для empty, populated і API error.

Acceptance: чистий checkout збирається командою `npm ci && npm run build`; `/logs` відкривається без `pageerror`.

### V2C-002. Виправити browser test Settings

Файли: `tests/test_browser_e2e.py`, `web-v2/src/app/settings/settings.component.ts`.

Роботи:

- визначити один семантичний елемент для поточної версії;
- додати стабільний `data-testid`;
- не змінювати перевірку фактичного значення версії;
- усунути strict locator collision.

Acceptance: усі наявні Browser E2E проходять; assertion перевіряє `0.0.1.19`, а не лише видимість секції.

### V2C-003. Зафіксувати baseline contracts тестами

Файли: `tests/`, `web-v2/src/app/core/api.service.ts`, `web-v2/src/app/core/models.ts`.

Роботи:

- додати contract coverage для Job, Worker/Node, Queue, Character, Workflow, Brand/Channel, Operations і System;
- перевірити точні payload names, enum states і response mappings;
- зафіксувати `scheduled_for`, `expected_version`, `READY`, GPU/VRAM metrics і node revoke.

## 5. Етап B — contracts і спільна інфраструктура

### V2C-101. Розділити API client за доменами

Створити typed clients: Jobs, Workers, Queue, Resources, Publishing, Operations, System, Setup. Тимчасовий facade дозволений для поетапної міграції компонентів.

Acceptance: production components не додають нових methods до монолітного `VertepApiService`; усі response types конкретні.

### V2C-102. Усунути `any` і невизначені system contracts

Пріоритетні файли:

- `web-v2/src/app/core/models.ts`;
- `web-v2/src/app/core/api.service.ts`;
- `web-v2/src/app/settings/settings.component.ts`;
- `web-v2/src/app/workers/workers.component.ts`;
- `web-v2/src/app/jobs/job-detail.component.ts`.

Описати `RuntimeMetrics`, `SelfTestResult`, `WorkflowDocument`, `IntegrationStatus`, `InstallationManifest`, `CertificateStatus`, `SecurityCheck`, `UpdateOperation`, `BackupSnapshot` і Telegram bot info.

### V2C-103. Уніфікувати remote і mutation state

Розширити shared primitives для явних `idle/loading/success/error`, retry, technical details і mutation progress. Перевести feature pages з дубльованих skeleton/error blocks на спільний contract.

### V2C-104. Permissions і system-state policy

Створити централізовану policy service/directive:

- `viewer` бачить read-only UI;
- `admin` бачить дозволені mutations;
- `MAINTENANCE`, `UPDATING`, `EMERGENCY`, `RECOVERING`, `READ_ONLY` блокують відповідні дії;
- disabled control показує причину.

Backend middleware і UI policy мають використовувати узгоджену матрицю дозволів.

## 6. Етап C — завершення Job lifecycle

### V2C-201. Завершити Job editor

Файли: `core/models.py`, `core/api/jobs.py`, `web-v2/src/app/jobs/job-detail.component.ts`.

Реалізувати структуроване редагування topic, character, priority, workflow, script і prompt кожної scene. Зберегти `expected_version`; для HTTP 409 показувати актуальну та локальну версії з явним reload/reapply flow.

### V2C-202. Структурований timeline

Backend має повертати event records із timestamp, type, state, attempt, worker/node, task і error. UI не повинен вигадувати timestamp із `created_at` Job. Додати links до Worker і пов'язаних artifacts.

### V2C-203. Завершити Artifacts workspace

Додати media preview для підтримуваних image/audio/video artifacts, окремий integrity state, повторну перевірку, download та зрозумілий invalid/corrupt state. Verify одного artifact не повинен маскувати стан інших.

### V2C-204. Завершити Publish flow

Реалізувати:

- вибір налаштованих enabled channels;
- credential readiness;
- approve-before-publish policy;
- progress/status polling;
- platform receipt та URL;
- retry лише failed channel;
- зрозумілий partial-success state.

Додати contract tests для Publisher adapters та Browser E2E із контрольованим test adapter. Live acceptance залишається окремим production check.

### V2C-205. Published query contract

Додати backend filters/pagination для published Jobs і publication results. UI не повинен завантажувати всі Jobs для локального фільтрування.

## 7. Етап D — Operations і Queue

### V2C-301. Завершити Queue page

Перевірити scheduled, ready, inflight і dead-letter contracts, pagination, retry conflicts та refresh. Додати E2E для кожного queue state.

### V2C-302. Завершити Alerts lifecycle

Додати Job і Worker links. Якщо acknowledgment входить до продуктового contract, реалізувати persistent alert ID, acknowledged_at/actor і backend mutation; інакше явно зафіксувати alerts як derived read-only view.

### V2C-303. Завершити Logs

Залежить від V2C-001. Додати polling/SSE, pause live updates, filter persistence, Worker/Job links, exception details і обмеження DOM rows.

### V2C-304. Health і monitoring acceptance

Перевірити `/api/health`, `/api/health/history`, `/api/metrics`, Grafana URL і degraded states. Додати Browser E2E для healthy/degraded/API error.

## 8. Етап E — Workers, Roles і deployment

### V2C-401. Worker onboarding acceptance

Завершити flow role → one-time token → Core URL/certificate → node polling → registration → self-test. Token має показувати TTL і не зберігатися після закриття wizard.

### V2C-402. Типізувати Worker detail і metrics

Прибрати читання hardware/runtime через довільні records. Відображати GPU, VRAM total/free, temperature, GPU/CPU load, RAM/disk, heartbeat, current Job/task, workflows, capabilities, runtime version і certificate expiry.

### V2C-403. Реалізувати Worker controls повністю

Файли: `core/api/nodes.py`, Worker runtime protocol, `worker-detail.component.ts`.

- замінити `logs: pass` переходом до `/logs?node_name=...` або реальним response;
- переконатися, що restart/update команди споживаються Worker, а не лише змінюють запис CORE;
- додати state guards для drain/resume/quarantine/self-test/disable/enable/restart/update/revoke;
- показувати command acknowledgment і timeout/error.

### V2C-404. Roles deployment progress

Після `POST /api/system/roles` показувати operation ID, executor progress, services, health result і помилку rollback. Не вважати зміну checkbox успішним deployment.

## 9. Етап F — Resources і Publishing settings

### V2C-501. Повний Character editor

Замість read-only JSON у Character Detail реалізувати редагування metadata, `system_prompt`, voice, visual, generation, workflow, retries і publishing. ID генерує система та лишається immutable. Додати schema validation і unsaved-changes guard.

### V2C-502. Workflow editor і validation

Додати form/JSON modes, backend schema validation, повний usage graph для Character і Job references та безпечне видалення з HTTP 409 details.

### V2C-503. Завершити Channels CRUD

Додати update/delete, enabled state, platform metadata, target validation, credential readiness і test publish. Не зберігати credentials у channel metadata.

### V2C-504. Telegram management

Створити окремий route `/telegram` із setup/status/bot-info, webhook/polling mode, allowed/admin chat IDs, connection test та diagnostics. Перевірити `/character` command наскрізним backend test.

## 10. Етап G — Settings, Update і Recovery

### V2C-601. Secrets Store

Створити route `/settings/secrets` із platform grouping, configured/missing state, write-only form, rotate і delete confirmation. Прибрати `window.prompt`; секрет ніколи не повертати у response або DOM після submit.

### V2C-602. Providers і Models

Реалізувати provider configuration через дозволений deployment/config flow, text model list/pull/delete, pull progress, voice list і synthesis preview. Розділити unavailable, empty і API error.

### V2C-603. Update Center

Створити route `/settings/update` із check, readiness, install, controlled restart, rolling start/status/cancel, canary promote/rollback та update rollback. Показувати drain acknowledgments, active Jobs, busy/unacknowledged Workers і operation log. Прибрати `window.location.reload()`.

### V2C-604. Backup і Recovery

Створити route `/settings/backups`. Перед restore показувати конкретний snapshot, поточний system state, backup metadata і очікувані наслідки. Після операції показувати progress та health verification. Додати recovery-to-NORMAL і update rollback як різні дії.

### V2C-605. General settings

Створити UI для integrations health, dashboard logo upload/delete, installation manifest, certificate status/renewal і security check. Технічні помилки показувати без витоку секретів.

## 11. Етап H — Angular Setup Wizard і Zero Shell

### V2C-701. Angular First Run Wizard для Core

Додати route `/setup`, доступний до AuthGuard за setup token. Перенести role, installation, admin, hardware, AI backend, health, manifest і done з legacy `web/setup.html` без втрати backend security checks.

### V2C-702. Non-Core enrollment

Реалізувати Core URL, Core certificate, registration token, CSR, credential persistence, enrollment diagnostics та появу вузла в Core. Покрити GPU/Text/Voice/Publisher/Backup/Monitoring roles.

### V2C-703. Навігація і lazy routes

Цільові routes:

- `/publishing`;
- `/telegram`;
- `/operations` з Alerts/Logs/Health;
- `/settings/secrets`;
- `/settings/roles`;
- `/settings/update`;
- `/settings/backups`;
- `/setup`.

Перевести feature routes на `loadComponent` або `loadChildren`; Sidebar формувати за permissions і capabilities. Видаляти legacy navigation лише після feature parity.

### V2C-704. Accessibility, localization і responsive QA

Перевірити keyboard navigation, focus trap, labels, dialog semantics, contrast, mobile tables/forms і послідовну українську термінологію. Допустимі технічні назви API, Job, Worker, Publisher, workflow і node roles.

## 12. Етап I — Acceptance і release readiness

### V2C-801. Feature Browser E2E

Для кожної feature page додати happy path, empty state, API error і critical mutation. Використовувати стабільні `data-testid`; кожен тест перевіряє відсутність `pageerror`.

### V2C-802. Наскрізний admin acceptance

Сценарій:

```text
setup Core
→ login
→ Worker onboarding і self-test
→ Character
→ Workflow
→ Brand і Channel
→ Job create/edit
→ lifecycle execution
→ approve
→ publish
→ publication receipt
→ update readiness
→ backup
→ recovery verification
```

### V2C-803. Non-Core acceptance

Перевірити bootstrap/enrollment, heartbeat, metrics, task claim/result, drain/resume, self-test, update, restart і revoke для кожної підтримуваної node role.

### V2C-804. Release qualification

Виконати повний набір перевірок із розділу 3 у чистому checkout і Docker build для `linux/amd64` та `linux/arm64`. Переконатися, що runtime bundle містить зібраний Web UI V2 і всі routes відкриваються через CORE fallback.

## 13. Таблиця прогресу

Статуси: `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`.

| Task | Статус | Залежності |
|---|---|---|
| V2C-001 Logs у source tree | DONE | — |
| V2C-002 Settings browser test | DONE | — |
| V2C-003 Baseline contract tests | DONE | — |
| V2C-101 Domain API clients | DONE | V2C-003 |
| V2C-102 Повна типізація | DONE | V2C-101 |
| V2C-103 Shared state | DONE | V2C-101 |
| V2C-104 Permissions/state policy | DONE | V2C-101 |
| V2C-201 Job editor | DONE | V2C-102, V2C-104 |
| V2C-202 Timeline | DONE | V2C-102 |
| V2C-203 Artifacts | DONE | V2C-102, V2C-103 |
| V2C-204 Publish flow | DONE | V2C-104, V2C-503 |
| V2C-205 Published query | DONE | V2C-204 |
| V2C-301 Queue | DONE | V2C-102, V2C-103 |
| V2C-302 Alerts | DONE | V2C-103 |
| V2C-303 Logs completion | DONE | V2C-001, V2C-103 |
| V2C-304 Health/monitoring | DONE | V2C-103 |
| V2C-401 Worker onboarding | DONE | V2C-102, V2C-104 |
| V2C-402 Worker metrics | DONE | V2C-102, V2C-401 |
| V2C-403 Worker controls | DONE | V2C-402, V2C-104 |
| V2C-404 Roles deployment | DONE | V2C-104 |
| V2C-501 Character editor | TODO | V2C-102, V2C-103 |
| V2C-502 Workflow editor | TODO | V2C-102, V2C-501 |
| V2C-503 Channels CRUD | TODO | V2C-104, V2C-601 |
| V2C-504 Telegram | TODO | V2C-104, V2C-601 |
| V2C-601 Secrets Store | TODO | V2C-102, V2C-104 |
| V2C-602 Providers/models | TODO | V2C-601 |
| V2C-603 Update Center | TODO | V2C-104, V2C-302, V2C-404 |
| V2C-604 Backup/recovery | TODO | V2C-603 |
| V2C-605 General settings | TODO | V2C-601 |
| V2C-701 Core Setup Wizard | TODO | V2C-104, V2C-601 |
| V2C-702 Non-Core enrollment | TODO | V2C-401, V2C-701 |
| V2C-703 Navigation/lazy routes | TODO | V2C-504, V2C-601, V2C-603, V2C-604 |
| V2C-704 Accessibility/UX | TODO | усі feature tasks |
| V2C-801 Feature Browser E2E | TODO | паралельно з feature tasks |
| V2C-802 Admin acceptance | TODO | V2C-704, V2C-801 |
| V2C-803 Non-Core acceptance | TODO | V2C-702, V2C-801 |
| V2C-804 Release qualification | TODO | V2C-802, V2C-803 |

## 14. Рекомендована послідовність

1. V2C-001, V2C-002, V2C-003 — повернути зелений baseline.
2. V2C-101—V2C-104 — contracts, state і permissions.
3. V2C-201—V2C-205 — завершити Job та Publisher lifecycle.
4. V2C-301—V2C-304 — Queue і Operations.
5. V2C-401—V2C-404 — Worker fleet та deployment roles.
6. V2C-501—V2C-504 — Characters, Workflows, Channels і Telegram.
7. V2C-601—V2C-605 — Settings, Update і Recovery.
8. V2C-701—V2C-704 — Setup і Zero Shell UX.
9. V2C-801—V2C-804 — наскрізна перевірка та release qualification.

Browser E2E з V2C-801 додаються разом із кожною feature task, а не відкладаються до кінця.

## 15. Журнал рішень

| Дата | Task | Рішення | Підстава |
|---|---|---|---|
| 2026-09-08 | PLAN | Створено новий completion plan | Аудит commit `3255a7a5`: build failure, незавершені `DONE`, 8 початкових `TODO` |
| 2026-09-08 | V2C-001 | Logs повернуто в активну роботу | `.gitignore` виключив Angular source directory з release commit |
| 2026-09-08 | V2C-002 | Усунуто strict locator collision у Settings | Додано окремі `data-testid` і збережено перевірку конкретних значень версій |
| 2026-09-08 | POLICY | Старі позначки `DONE` не успадковуються автоматично | Нова готовність підтверджується backend, UI, contracts і Browser E2E |
| 2026-09-08 | V2C-401 | Worker onboarding wizard — E2E, data-testid, contract tests | Token TTL, polling, wizard clear |
| 2026-09-08 | V2C-402 | NodeDetail типізовано через WorkerHardware/WorkerRuntime | Record<string, unknown> замінено на specific interfaces |
| 2026-09-08 | V2C-403 | NodeAction regex розширено (update, rotate, revoke) | State guards у worker-detail via availableActions computed |
| 2026-09-08 | V2C-404 | Roles deployment progress — polling, status message, services | RolesDeploymentState/RolesUpdateResponse моделі |
