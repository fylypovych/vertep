# Локальна перевірка #363 / #364

База: `a98e2de4a19a6906c9403e8495a37275baa8f445`, `main`, VERSION `0.0.2.87`.
Перевіряється робоче дерево зі збереженими попередніми змінами #360–362.
Tracked diff SHA256: `5973f1fc5a54238b7dc03d8859dbd40e132d0d5a0fcbaee0e07a39b439f6a476`.
Це digest acceptance-прогону. Після нього ізольовано `CONFIG_ROOT` двох
наявних unit-fixtures; runtime-код і нові process-тести не змінювалися.
Digest остаточного regression-прогону:
`017df6e7cc530fc7266e9f33b1a93f3db1e389a1807994c102ac43486b58143c`.
JUnit зберігає SHA бази, digest diff і SHA допоміжних process-файлів;
артефакти локальні в `.pytest_tmp/issue-363-364-*`.

## #363

| Критерій | Доказ |
| --- | --- |
| Persist → deliver → apply → verify, task, restart | `test_supported_switch_task_restart_and_failed_activation_rollback`: окремі CORE/Worker, stock heartbeat, фактичний speech task, нові PID після restart, повторний task |
| Невдала активація і rollback | Той самий тест: CORE overrides та Worker повертаються до попереднього backend; повторний task. `test_deferred_activation_is_not_success_and_late_failure_rolls_back`: без Worker повертається 202; пізня помилка не стає успіхом |
| MPT, snapshot, секрети | `test_actual_settings_mpt_task_restart_rollback_and_native`: фактичний FFmpeg render через HTTP runtime, snapshot із CORE, restart, відмова пошкодженого результату й недоступного MPT без Native fallback, rollback та явний Native. Наявні `test_assembly_worker_route.py` перевіряють незмінність dispatched attempt |
| Browser / RBAC / state | Справжні Settings та encrypted secret form, HTTP mutations без `page.route`; admin/viewer, 403/423, redaction DOM/API/jobs/logs, pageerror/console |

Змінено спільний `core/provider_switch.py`, heartbeat models/API та `worker/service.py`,
engine secret-reference comparison, Settings API/UI. Транзакція має журнал;
непідтверджене відновлення повертає 503. Busy Worker не змінює конфігурацію активного task.
Credentials залишаються в encrypted CORE store та node-local provisioning.

## #364

| Критерій | Доказ |
| --- | --- |
| Enrollment retry після commit | `test_stock_enrollment_lost_response_after_commit_renew_revoke`: stock retry в окремому процесі, proxy обриває першу відповідь після CORE commit; одна node identity, старий JWT відхилено; renew/revoke через API |
| Сім ролей і Full Self-Test | `test_measured_seven_role_runtime_api_fleet_and_dispatch`: CORE з реальними PG/Redis; шість Worker-role self-test через stock calculation, HTTP providers, media/hash/receipt/storage evidence; enrollment/cert/heartbeat, API/Fleet/dispatch, stale/revoked negative; справжній script task/result; authenticated confirmed Full Self-Test та durable audit |
| Setup Browser | `test_setup_browser_deployment_manifest_enrollment_and_reload`: фактично served First Run UI, failure/retry, deployment request, stock apply script, manifest/done, enrollment, reload/restart. `test_fleet_browser_token_enrollment_retry_and_forbidden_mutations`: реальні 403/423 і retry |
| Required dependencies | `test_required_services_and_optional_backend_use_actual_probes`: SQL SELECT 1 та Redis PING, обрані role/backend checks; TCP listener замість PostgreSQL не проходить; optional Ollama не блокує CORE. Vendor command exit/output перевірено окремо |

Змінено існуючі setup/health/nodes/workers/system API, role runtime, certificate probe,
task ledger read model, FileRepository locking, confirmed Telegram test outcome та Setup/Fleet UI.
Немає нового deployment engine чи окремого механізму доставки provider config.

## Середовище та межі доказів

- Python 3.10.11, Node 24.15.0, Playwright 1.62.0 Chromium, OpenSSL 3.5.8,
  Redis 8.10.3, PostgreSQL 17.6; справжні ізольовані CORE/Worker HTTP процеси.
- MPT runtime — керований HTTP stand-in із pinned contract і справжнім FFmpeg;
  це не доказ upstream deployment із #365.
- Docker CLI — керований зовнішній executable; stock deployment orchestration виконується,
  але Docker daemon/image build цим контуром не доведено.
- GPU task використовує керований HTTP adapter з фактичним PNG; vendor CLI negative checks
  не є доказом фізичного GPU deployment.
- Сім ролей перевіряються фактичними role self-test та CORE protocol; це не сім окремих Worker daemons.
- Telegram transport підмінено на межі адаптера; повідомлення користувачам не надсилалися.
  Active fencing/restart orchestration #372 та shared CORE extraction #366 не розширювалися.
- Наявний Windows skip POSIX `systemctl` stub у regression suite не зараховується як успіх.

## Відтворення

Встановити `VERTEP_TEST_POSTGRES_BIN` на каталог PostgreSQL binaries,
додати Redis/OpenSSL/Node до PATH та встановити
`VERTEP_REQUIRE_NODE_PROCESS=1`, `VERTEP_REQUIRE_MEDIA_BROWSER_E2E=1`.

```powershell
python -m pytest -q tests/test_provider_process_acceptance.py tests/test_node_process_acceptance.py --tb=short -o junit_family=legacy --junitxml=.pytest_tmp/issue-363-364-process-reviewed.xml
npm.cmd --prefix web-v2 run build
python -m compileall -q core adapters worker scripts installer tests
git diff --check
```

Локальний PASS не є green exact-SHA GitHub Browser CI: зміни ще незакомічені,
тому remote CI для їхнього SHA відсутній. Цей пункт приймання залишається
непідтвердженим до окремо доручених push і перевірки CI; він не є дозволом на push.
У проміжному повторі був одиничний 409 під час позитивної TTS activation;
окремий повтор та три послідовні додаткові повтори пройшли. Додано безпечне
логування класу неочікуваної помилки без її значень/credentials.

Результати:
- Process/Browser acceptance: **16 passed, 0 skipped**, 206.69 s;
  `.pytest_tmp/issue-363-364-final-stable.xml` та `.log`.
  Для AC1–4 обох Issues expected — успішні positive assertions і відхилені
  negative mutations; actual — усі відповідні test IDs пройшли.
- Setup Browser після доданого очікування фактичного node у Fleet: **1 passed**,
  `.pytest_tmp/issue-364-setup-reviewed.log` та `setup-fleet.png` у відповідному basetemp.
- Node readiness / role runtime / engine / assembly після ізоляції стану:
  **112 passed**, `.pytest_tmp/issue-363-364-isolation.xml`.
- Web build, compileall, `git diff --check`: **PASS**; Angular має наявні template warnings.
- Спільний regression до ізоляції fixtures: **461 passed, 16 failed, 1 skipped**;
  усі failures в assembly suite, яка окремо проходила.
- Остаточний спільний regression після ізоляції fixtures: **477 passed, 1 skipped**,
  342.50 s; `.pytest_tmp/issue-363-364-regression-final.xml` та `.log`.
  Невирішених failures цього прогону немає; skip — POSIX-only restart stub на Windows.

Статус локальної реалізації: критерії AC1–4 підтверджені наведеними disposable
перевірками. Загальне приймання **PARTIAL** лише через відсутність green exact-SHA
remote Browser CI для ще не опублікованих змін; нову версію/commit самостійно не створено.

Commit/push/release і зміни статусів GitHub Issues не виконувалися.
