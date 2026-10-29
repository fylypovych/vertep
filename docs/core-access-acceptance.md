# CORE access: приймання #362 (i.0.0.3.62)

Локальна реалізація поверх main `a98e2de4a19a6906c9403e8495a37275baa8f445` (0.0.2.87), робоче дерево зі змінами попередніх #360/#361. Це не результат опублікованого SHA. JUnit для нових acceptance tests містить `source_sha`, `tracked_diff_sha256`, `test_sha256` і `source_status`; production/release не перевірялися.

## Погоджена матриця AC2

Клітинки нижче обмежені ресурсами й станами #168.3–4/#222. Viewer у READ_ONLY/RECOVERING отримує 423: центральний state guard виконується раніше role guard. Це чинний порядок, а не новий дозвіл.

Позначення API (усі в `tests/test_system_state_mutation_matrix.py`, крім remaining):
- `existing-positive` → `test_the_administrator_probe_changes_the_resource_it_guards[resource]`; models → `test_the_model_pull_probe_keeps_its_method_and_path` плюс чинний `test_model_lifecycle.py::test_model_pull_endpoint_is_idempotent_for_active_operation` (200/та сама активна operation), `test_node_pull_command_progress_and_completion` і `test_model_pull_http.py::test_local_http_stream_success_completes_pull` (реальний HTTP stream зовнішнього model service).
- `existing-viewer` → `test_a_viewer_mutation_is_refused_and_leaves_the_resource_unchanged[resource]`.
- `existing-readonly` → `test_a_blocked_state_leaves_the_resource_unchanged[resource]`.
- `remaining` → `tests/test_core_access_acceptance.py::test_remaining_resource_cells_preserve_actual_state[resource-role-state-423]`.
- `B` → `tests/test_core_access_browser_e2e.py::test_browser_role_state_resource_matrix`. Browser використовує справжні cookies/CSRF, HTTP CORE, перевіряє guard і повторює canonical mutation; перед/після порівнює вміст resource files, channels repository і model operations. Для viewer models адмінський Settings недоступний; запит виконується з автентифікованої сторінки брендів. Готова API реалізація не переписується.

| Ресурс | Роль | Стан | Expected status / mutation | API test ID | Browser test ID |
|---|---|---|---|---|---|
| characters | admin | NORMAL | 200; зміна дозволена | existing-positive | Чинний API positive; Browser не дублюється |
| characters | viewer | NORMAL | 403; без зміни | existing-viewer | B[characters-viewer-NORMAL-403] |
| characters | admin | READ_ONLY | 423; без зміни | existing-readonly | B[characters-admin-READ_ONLY-423] |
| characters | viewer | READ_ONLY | 423; без зміни | remaining | B[characters-viewer-READ_ONLY-423] |
| characters | admin | RECOVERING | 423; без зміни | remaining | B[characters-admin-RECOVERING-423] |
| characters | viewer | RECOVERING | 423; без зміни | remaining | B[characters-viewer-RECOVERING-423] |
| brands | admin | NORMAL | 200; зміна дозволена | existing-positive | Чинний API positive; Browser не дублюється |
| brands | viewer | NORMAL | 403; без зміни | existing-viewer | B[brands-viewer-NORMAL-403] |
| brands | admin | READ_ONLY | 423; без зміни | existing-readonly | B[brands-admin-READ_ONLY-423] |
| brands | viewer | READ_ONLY | 423; без зміни | remaining | B[brands-viewer-READ_ONLY-423] |
| brands | admin | RECOVERING | 423; без зміни | remaining | B[brands-admin-RECOVERING-423] |
| brands | viewer | RECOVERING | 423; без зміни | remaining | B[brands-viewer-RECOVERING-423] |
| workflows | admin | NORMAL | 200; зміна дозволена | existing-positive | Чинний API positive; Browser не дублюється |
| workflows | viewer | NORMAL | 403; без зміни | existing-viewer | B[workflows-viewer-NORMAL-403] |
| workflows | admin | READ_ONLY | 423; без зміни | existing-readonly | B[workflows-admin-READ_ONLY-423] |
| workflows | viewer | READ_ONLY | 423; без зміни | remaining | B[workflows-viewer-READ_ONLY-423] |
| workflows | admin | RECOVERING | 423; без зміни | remaining | B[workflows-admin-RECOVERING-423] |
| workflows | viewer | RECOVERING | 423; без зміни | remaining | B[workflows-viewer-RECOVERING-423] |
| models | admin | NORMAL | 200; зміна дозволена | existing-positive | Чинний API positive; Browser не дублюється |
| models | viewer | NORMAL | 403; без зміни | existing-viewer | B[models-viewer-NORMAL-403] |
| models | admin | READ_ONLY | 423; без зміни | existing-readonly | B[models-admin-READ_ONLY-423] |
| models | viewer | READ_ONLY | 423; без зміни | remaining | B[models-viewer-READ_ONLY-423] |
| models | admin | RECOVERING | 423; без зміни | remaining | B[models-admin-RECOVERING-423] |
| models | viewer | RECOVERING | 423; без зміни | remaining | B[models-viewer-RECOVERING-423] |
| channels | admin | NORMAL | 200; зміна дозволена | existing-positive | Чинний API positive; Browser не дублюється |
| channels | viewer | NORMAL | 403; без зміни | existing-viewer | B[channels-viewer-NORMAL-403] |
| channels | admin | READ_ONLY | 423; без зміни | existing-readonly | B[channels-admin-READ_ONLY-423] |
| channels | viewer | READ_ONLY | 423; без зміни | remaining | B[channels-viewer-READ_ONLY-423] |
| channels | admin | RECOVERING | 423; без зміни | remaining | B[channels-admin-RECOVERING-423] |
| channels | viewer | RECOVERING | 423; без зміни | remaining | B[channels-viewer-RECOVERING-423] |

Models NORMAL: 200 стосується чинного configured-node pull acceptance. Локальний `/api/models/text/pull` probe без model service доводить method/path, а не успішне завантаження моделі. Негативні клітинки цього endpoint перевіряють також відсутність нових operation/файлів. Успішна модельна orchestration зараховується чинними lifecycle/HTTP tests, не підміняється green route assertion.

## AC1: чинні contracts

| Критерій | Test ID / реалізація | Expected |
|---|---|---|
| POST /api/session, GET/POST /api/system/state | `test_api_client_contracts.py::test_session_client_endpoints_exist`; route/state матриця `test_route_classification_matches_state_policy` | Чинні method/path, без 404/405 |
| UTF-8 Basic credentials, cookies, negative auth | `test_backend_contracts.py`: `test_utf8_basic_credentials_open_a_session`, `test_invalid_credentials_never_open_a_session`, `test_unset_admin_password_never_mints_a_session`, `test_unset_session_secret_rejects_forged_session` | Чинна автентифікація без fallback |
| CSRF | `test_features.py::test_cookie_session_requires_csrf`; новий Browser session test | Запит без CSRF 403, запис профілю незмінний; valid CSRF 200 |
| Restore classification | `test_system_state_mutation_matrix.py::test_restore_is_not_misclassified_as_backup` | Restore класифікується раніше загального backup prefix |

Session/router extraction не виконується: власник #366.

## AC3: remaining actual backend Browser

Усі ID нижче в `tests/test_core_access_browser_e2e.py`. CORE працює через uvicorn, Web — production Angular build у Chromium; `page.route` і підміни CORE JSON відсутні.

| Test ID | Expected status / persisted outcome / UI |
|---|---|
| `test_browser_session_profile_csrf_logout_in_restricted_mode[mode-role]`, mode=RECOVERING/READ_ONLY/EMERGENCY, role=admin/viewer | Справжній POST session 200; UI PUT profile 200 з українським ім’ям; без CSRF 403 без запису; DELETE session 200; наступний GET 401 |
| `test_browser_create_job_waits_without_dispatch[mode]`, mode=MAINTENANCE/UPDATING/RECOVERING | UI POST jobs 200; persisted WAITING_FOR_SYSTEM; queue depth не зростає; UI «Очікує систему» |
| `test_browser_create_job_denied_without_persistence[admin-READ_ONLY-423]`, `[admin-EMERGENCY-423]`, `[viewer-NORMAL-403]` | Кнопка disabled; canonical POST має вказаний статус, нового job немає |
| `test_browser_recovery_denied_preserves_state[admin-RECOVERING-423]`, `[admin-READ_ONLY-423]`, `[viewer-EMERGENCY-403]` | Disabled recovery для admin; viewer не має Settings; canonical POST відхилено, state file byte-exact незмінний |
| `test_browser_recovery_health_gate_and_success` | EMERGENCY admin: справжній health check 409 при недоступному Postgres; state незмінний, UI показує помилку. Після доступності залежностей 200, persisted NORMAL, дозволений brand POST 200 і файл створено |
| `test_browser_status_transport_fault_and_recovery[loading]` / `[error]` | Справжній reverse proxy затримує /api/status або повертає HTTP503 без фальшивого JSON; UI «Невизначений»/«Недоступний», guard disabled, немає mutation HTTP/записів. Після відновлення мережі NORMAL, guard enabled, brand POST 200 із записом |
| `test_unknown_backend_record_is_visible_and_does_not_grant_writes` | Фактичний невідомий persisted state → EMERGENCY, UI «Аварія» з причиною; canonical brand POST 423, ресурси незмінні |

Recovery positive використовує справжній disposable Redis і контрольований зовнішній TCP listener на 127.0.0.2:5432. Чинний Postgres health contract лише відкриває TCP socket; цей тест доводить health gate і recovery mutation, не SQL persistence. Redis відсутній у mandatory Browser CI → FAIL, не зарахований skip.

## Виправлені дефекти

- Невідомий/пошкоджений state file більше не повертає дозвільний NORMAL; використано чинний EMERGENCY. Відсутній початковий файл зберігає початковий NORMAL.
- Header не показує NORMAL/зелений стан до першої authoritative відповіді; відповідь/error явно запускає оновлення відображення в Angular.
- Кнопка recovery та handler використовують чинну `start_recovery` policy, після успіху policy оновлюється. Нових permissions/routes немає.

До виправлення збережено відтворення: `.pytest_tmp/issue-362-unknown-before.log` (недозволений POST повертав 200), `.pytest_tmp/issue-362-recovery-before.log` (кнопка enabled у RECOVERING).

## Команди та артефакти

- API/contracts/regressions: `.pytest_tmp/issue-362-api-final.log`, `.pytest_tmp/issue-362-api.xml`.
- Browser: `.pytest_tmp/issue-362-browser-final.log`, `.pytest_tmp/issue-362-browser.xml`, `.pytest_tmp/issue-362-confirmed/` (screenshots/матриця).
- Production Web build: `.pytest_tmp/issue-362-web-build.log`.
- CI: чинний surface acceptance job запускає обидва нові файли, existing state/client contracts; Redis dependency встановлюється перед запуском, JUnit/screenshots/matrix JSON зберігаються з SHA workflow.

Середовище: Windows, Python 3.10.11, Node 24.15.0, Playwright 1.62.0/Chromium, disposable Redis 8.10.3; CORE file repository й тимчасові resource/config roots. Remote CI цим дорученням не запускався.

Команди перевірок (portable Node/Redis додано до PATH, `VERTEP_REQUIRE_MEDIA_BROWSER_E2E=1`):

```text
python -m pytest -q tests/test_core_access_acceptance.py tests/test_system_state_mutation_matrix.py tests/test_api_client_contracts.py tests/test_primary_admin_role.py tests/test_backend_contracts.py tests/test_features.py tests/test_local_contracts.py tests/test_model_lifecycle.py tests/test_model_pull_http.py tests/test_worker_model_commands.py --tb=short -o junit_family=legacy --junitxml=.pytest_tmp/issue-362-api.xml
python -m pytest -q tests/test_core_access_browser_e2e.py tests/test_web_state_browser_e2e.py --tb=short -o junit_family=legacy --junitxml=.pytest_tmp/issue-362-browser.xml --basetemp=.pytest_tmp/issue-362-confirmed
npm --prefix web-v2 run build
python -m compileall -q core adapters worker scripts installer tests
git diff --check
```

Browser: **56 passed**, без failures/skips: 44 нові actual-backend access cases та 12 чинних Web state regressions. Усі 56 мають source SHA `a98e2de4a19a6906c9403e8495a37275baa8f445` і tracked diff SHA256 `01cbb4fc3943d2042303437193a8e8a2108c55bf83b04c7e416714e9d099b061` у JUnit. Header/matrix screenshots перевірено візуально після authoritative відповіді, не під час початкового loading.

API regressions остаточного стану: **342 passed**, без failures/skips (123.89 s). Production Web build, compileall та `git diff --check`: **PASS**.

| Критерій | Actual | Локальний результат |
|---|---|---|
| AC1 | Чинні session/state methods, UTF-8, cookies/CSRF і restore classification відповідають expected; 342 regressions PASS | PASS |
| AC2 | 30 погоджених resource cells прив’язані до existing/remaining test IDs; негативні HTTP status і відсутність mutation підтверджені | PASS |
| AC3 | 44 actual-backend Browser access cases PASS: 25 resource guards, 6 sessions, unknown, 6 create-job, 4 recovery та 2 transport faults | PASS |

Статус технічного виконання: **IMPLEMENTED локально**. Опублікованого main SHA з цими змінами немає; це не твердження про виконання release/main acceptance та не підстава закривати Issue.

Файли цього доручення: `core/system_state.py`, `web-v2/src/app/layout/header.component.ts`, `web-v2/src/app/settings/sections/update.section.ts`, `.github/workflows/browser-e2e.yml`, `tests/test_core_access_acceptance.py`, `tests/test_core_access_browser_e2e.py`, цей документ. Попередні незакомічені зміни та початкові видалення release evidence збережені. Commit/push/release, зміни GitHub Issue не виконувалися.

