# Health і застосування ролей — #449 та #450

> Статус: матеріал приймання; результат і межі наведено в документі | Аудиторія: розробник, ревізор | Канон: [AGENTS.md](../../AGENTS.md).

## Рішення щодо інтерфейсу

Health показує поточні віджети служб, що оновлюються на місці, та до десяти останніх змін стану. Повторювані сусідні знімки з однаковими перевірками не дають оператору нової інформації, тому згортаються лише при відображенні. Зміна набору ролей, обов’язкових перевірок, результату або причини утворює окремий запис. Незмінні минулі відмови залишаються видимими. Повний `health-history.jsonl` зберігається без видалення чи переписування; API повертає оригінальні записи, пропускаючи пошкоджені рядки. Інтерфейс не перераховує минулі результати за сьогоднішнім набором ролей.

Для ролей збережено checkbox і спільне збереження набору. Executor застосовує один Compose-план із загальними Worker та залежностями; окремі кнопки для кожної ролі створювали б враження незалежних операцій. Пропозицію кнопок та відсотків оцінено, але не прийнято: тривалість pull і self-test не визначає чесного відсотка готовності. Показуються фактично записані етапи підготовки, завантаження, запуску, перевірки та вимкнення служб. Під час операції повторне застосування заблоковане.

## Встановлені причини в коді

- Health приводив усі значення `checks` до boolean-списків: `role`, `checked_at`, `required` ставали псевдоперевірками, `null` показувався як FAIL. Тепер показуються лише пари `[boolean | null, string]`, з локалізованими назвами, станами й відомими причинами. Технічна причина доступна як tooltip.
- Roles завантажував результат лише після save, зберігав requested тільки у пам’яті компонента та підписував вибір як активні ролі. Polling тепер працює й після reload; завершення оновлює Angular через `markForCheck`. Незбережений вибір зберігається під час polling.
- План executor записується до перевірки служб, тому сам план не доводить застосування. API під час APPLYING повертає попередній застосований набір окремо від requested, ідентифікатор запиту корелює його зі статусом. SUCCEEDED означає завершення розгортання; поточна runtime-готовність вимірюється окремо. Готовність віддалених вузлів підписана окремо й не активує локальні checkbox.
- Помилки валідації до внутрішнього rollback-блоку раніше могли залишати попередній статус. Тепер вони записують FAILED. Неприйнятий понад 120 секунд запит або операція без завершення понад 4 години показують діагностику executor, не вигаданий успіх. Ці пороги не скасовують і не запускають операцію повторно.
- Атомарна публікація без перезапису захищає запит між API-процесами. Executor використовує спільний з update lease; конкурент не споживає чужий запит. Наявні rollback та архівування невдалого запиту збережено. Успішне вимкнення перевіряє відсутність запущених зайвих служб.

Це підтверджені дефекти локального коду. Причина конкретного інциденту на `10.10.10.15` не встановлена: його SHA, журнал executor і стан контейнерів не отримувалися. Наявний role-aware backend уже відбирав перевірки за конфігурацією; припущення, що будь-який історичний GPU FAIL є помилкою backend, не підтверджено.

## Автоматизоване приймання

| Issue / критерії за порядком у GitHub | Статус | Доказ реалізації |
| --- | --- | --- |
| #449: 1 | PASS | Рішення про віджети, зміни стану й незалежне збереження діагностики вище |
| #449: 2, 3, 5 | PASS | `test_history_keeps_failures_and_new_unselected_roles_do_not_fail`: GPU, Voice, Publisher, Text, Backup, Monitoring; зміна конфігурації та збереження справжньої відмови. `test_environment_readiness.py`: вибір backend, required/optional, застарілий environment |
| #449: 4, 6, 7 | PASS | Browser `Health відокремлює metadata, optional і минулу GPU відмову; uk/en`: metadata, null, optional, згортання повторів, українська й англійська |
| #450: 1, 5, 7, 8 | PASS | Browser-переходи Voice/Publisher окремо й разом, reload до й після результату, віддалена READY при локально вимкненій ролі, невибрана GPU, збережені Text/Backup/Monitoring |
| #450: 2, 3 | PASS | Рішення й підтверджені причини вище |
| #450: 4, 6, 9 | PASS | `test_executor_terminal_results_and_real_phase_boundaries`: потрібний набір Compose-сервісів, pull/start/health/prerequisite failures, кінцеві SUCCEEDED/FAILED; `test_stalled_executor_keeps_actual_state_and_diagnoses` та Browser діагностики executor |
| #450: 4, 7 | PASS | `test_request_reload_idempotency_and_conflict`, `test_competing_executor_cannot_consume_or_fail_the_owners_request`; існуючі rollback-тести `test_apply_deployment.py` |
| #450: 1, 4, 8 | PASS | `test_local_role_readiness_uses_current_http_result_not_saved_plan`: реальний локальний HTTP 503 → 200 для Voice/Publisher, DEGRADED → READY, конфігурація без GPU |

Межі доказів: Browser запускає справжній Chromium та Angular із підставленими API-відповідями. Executor-тести виконують штатний Python executor у тимчасовій директорії, але результати Docker команд моделюються. HTTP-тест використовує локальний тестовий сервер, не сторонній TTS чи обліковий запис публікації. Це автоматизоване приймання типу `i`, без твердження про готовність фізичного сервера або реальну публікацію. Результати отримано в робочому дереві з попередніми змінами; базовий HEAD `0cfbf961e714ed35ae368073361dd82ce5909d7d` не є SHA цих локальних виправлень.


## Змінені файли та перевірки

- Backend: `core/health_checks.py`, `core/role_runtime.py`, `core/deployment_state.py`, `core/api/observability.py`, `core/api/system.py`, `scripts/apply-deployment.py`.
- UI: `web-v2/src/app/health/health.component.ts`, `web-v2/src/app/health/health.presentation.ts`, `web-v2/src/app/settings/sections/roles.section.ts`, `web-v2/src/app/core/models.ts`, `web-v2/src/app/core/i18n/en.ts`.
- Тести: `tests/test_issues_449_450.py`, `tests/test_apply_deployment.py`, `web-v2/tests/settings-roles.spec.ts`, `web-v2/tests/health-roles-449-450.spec.ts`.

Перевірено 2026-11-11 у Windows, Python 3.10, Node 24.15.0:

- `python -m pytest -q tests/test_role_services.py tests/test_issues_449_450.py tests/test_role_runtime.py tests/test_apply_deployment.py tests/test_system_roles.py tests/test_environment_readiness.py tests/test_core_role_health_urls.py tests/test_monitoring_api.py --junitxml=.local/issue-449-450.xml --tb=short` — **182 PASS**, без skips. JUnit: `.local/issue-449-450.xml`.
- Chromium: `tests/settings-roles.spec.ts`, `tests/health-roles-449-450.spec.ts` — **10 PASS**, без skips.
- Angular production build, компіляція змінених Python-файлів та `git diff --check` — **PASS**. У build залишилися попередні попередження про optional chaining у Security/Update поза цим scope.
- Node 24.15.0 запускався через `npm exec --yes --package=node@24.15.0 -- node ...`; системна Node 24.14.0 відхиляється встановленим Angular CLI. Залежності проєкту не змінювалися.

Зміни локальні, без commit/push і без редагування чи закриття Issues.
