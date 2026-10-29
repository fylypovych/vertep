# Локальна реалізація #369 і #370

Дата: 29.10.2026. База: `main`, `64f2e6c6b58cc6c65601e7b2a3cde5d18251b845`
(`0.0.2.89`). Результати нижче стосуються **бази плюс незакомічені зміни**,
а не опублікованого SHA. Маніфест SHA256 робочих файлів:
`.pytest_tmp/issue-369-370-worktree.json`. Commit/push/закриття Issues не виконувалися.
Погодження #367 і L1/UX #370 збережені; повторне погодження не потрібне.

## #369 — i.0.0.3.69

| ID | Реалізація / test ID | Expected → actual | Локальний результат |
|---|---|---|---|
| AC1 | `worker/role_executor.py`, `worker/migration.py`, `worker/module_runtime.py`; `test_module_worker_route::test_stock_worker_shadow_switch_rollback[text/gpu/voice]` | Штатний `worker.service.execute_task` обирає role/shadow/module/role; реальні контрольовані Ollama/ComfyUI/TTS HTTP відповіді перетворюються на артефакти → усі три маршрути перевірено | PASS |
| AC2 | `worker/migration_trace.py`, `modules/execution.py`; той самий параметризований тест | Одна зовнішня inference на task, одна authoritative відповідь, однакові SHA256 outputs; shadow не пише module cache → 4 provider POST на 4 tasks, parity true, фактичний owner, rollback на role | PASS |
| AC3 | `core/backup/workflow.py`, `core/backup/{snapshot,storage,resume}.py`, CLI wrappers; `test_workflow_backup_execution::test_backup_cli_process_resume_preserves_approval_artifacts_and_module_state` | Checkpoint → CLI create/list/verify → mutation → restore → новий CORE process → новий Worker process → resume → COMPLETED; точні bytes module cache/artifacts та approval збережені | PASS |
| AC4 | `test_workflow_dispatch_execution::{test_worker_loss_falls_back_and_rejects_old_result,test_duplicate_owner_and_stale_lease,test_revision_invalidates_approval_and_descendants}`; `test_architecture_contract_integration::{test_dependency_range_and_capability_replacement,test_gpu_configured_priority_fallback_and_local_only,test_gpu_uncertain_submit_blocks_duplicate_and_capacity}`; `test_workflow_http_process::test_core_worker_http_dispatch_approval_restart`; `test_lab_resource_transport::test_revoke_after_production_work_stops_the_lab` | Worker loss/fallback, stale/revoked lease, revision approval, залежні outputs, local-only, сумісні/несумісні версії → перевірено фактичними Dispatcher/Engine/transport owners | PASS |
| AC4 metrics | `core/api/observability.py`; процесний backup test вище | Після фактичного Worker виконання його inventory передається heartbeat; HTTP `/api/metrics` та `/metrics` показують 1 execution/success → перевірено actual scrape | PASS |
| AC5 | `core/models.py`, `core/workflow/engine.py`; процесний backup test; `test_lab_resource_transport.py`, `test_workflow_provider_http::test_configured_provider_execution[lab]` | owner/project/ACL у checkpoint; авторизований versioned status/request/heartbeat/revoke/release/restart protocol → дані збережено, inference лише з lease | PASS |

Перемикачі на Worker: `VERTEP_TEXT_MODULE_ENABLED`, `VERTEP_IMAGE_MODULE_ENABLED`,
`VERTEP_AUDIO_MODULE_ENABLED`; для shadow відповідний суфікс `_MODULE_SHADOW`.
Default — legacy role. Для rollback обидва flags відповідного модуля `false`.
Shadow виконує ту саму бізнес-логіку вдруге, відтворюючи лише записані provider
відповіді; повторного зовнішнього inference немає. Shared role executor збережено.
Module cache: `MODULE_STATE_ROOT`; declarative paths — `modules/*/manifest.yaml`.
Restore виконують при зупиненому CORE/Worker; незавершені jobs відновлюються PAUSED,
старі RUNNING leases fenced, resume потребує нового dispatch.

## #370 — i.0.0.3.70

| ID | Реалізація / test ID | Expected → actual | Локальний результат |
|---|---|---|---|
| AC1/L2 | `lab/{budget,executor,orchestrator}.py`; `test_lab_executor::{test_run_reaches_completed_only_through_the_real_endpoint,test_missing_usage_stays_unknown_and_blocks_after_restart,test_returned_usage_exceeding_limit_fails_and_blocks_more_calls}`; `test_lab_budget.py`, `test_lab_orchestrator.py` | Реальний HTTP invoke, provider allowlist, request/token/timeout limits, journal/handoff/restart; відсутній usage залишається null та блокує наступний запит → перевірено, paid/disabled/error маршрути не дають fake success | PASS |
| AC2/L3 | `lab/{cli,api,gpu_lease,orchestrator}.py`, `core/gpu/backends.py`; `test_lab_executor::test_stock_resource_client_controls_real_role_invocation[False/True]`; `test_lab_resource_transport.py`; CLI resource tests | Штатна env-конфігурація → авторизований resource API; deny не викликає executor, grant передає lease, heartbeat до/після, release finally; expiry/revoke/loss/restart зупиняють використання → перевірено | PASS |
| AC3/L5 | `lab/{policy,state_store,governance,orchestrator}.py`; `test_lab_executor::test_exact_scope_approval_drives_one_task_and_retry`; `test_lab_cli::test_one_owner_grant_creates_exactly_one_issue_and_no_more`; `test_lab_github_transport::test_process_crash_after_remote_create_recovers_without_duplicate` | Exact scope owner approval, durable queue/grants/intents, blocked unapproved work, одна remote mutation попри смерть процесу після POST і повтори → один Issue/POST | PASS |
| AC4/L6 | `lab/evidence.py`, `lab/governance.py`; `test_lab_ir_acceptance.py`, `test_lab_github_transport.py`, `test_lab_web_e2e.py` | Full SHA + checksum machine report + expected/actual + owner-trusted independent Ed25519 attestation; mismatches відхилені. Усі successor numbers збережені до першого effect, remote body/coverage перевіряються перед numeric source close; retry не дублює Issue/comment → перевірено | PASS |
| AC5/L8 | `deploy/lab/Dockerfile.sandbox`, Compose, `lab/workspace.py`; `test_lab_sandbox_runtime::test_built_sandbox_rejects_host_access` | Actual build/run, nonroot, read-only root, dropped capabilities, no-new-privileges, private tmpfs, network none, без host binds → filesystem/command/socket/secrets/network заборонені, дозволена локальна команда працює | PASS |

Для `i`/`var` creation digest `issue_scope(kind,title,identifier,metadata)` передається
в owner grant як `scope_version`; відмова повертає `approval_scope` для review.
Task enqueue також повертає digest, потрібний grant `execute_approved_task`.
Idempotency key не можна використати для іншого scope. Неоднозначний remote failure
залишається uncertain до перевіреного read-back; повторний POST навмання заборонений.

Штатний resource client використовує `LAB_RESOURCE_URL`, `LAB_RESOURCE_TOKEN`,
`LAB_IDLE_SHARING_ENABLED`; API та CLI не створюють scheduler. Fake CLI provider
дозволений лише при явних `LAB_GPU_FAKE=true` **і** `LAB_DEMO_MODE=1`.
Непідтверджений usage після crash не перетворюється на нуль і не запускає новий AI call.

Evidence файли лежать у `LAB_EVIDENCE_ROOT`; незалежні public keys — в окремому,
керованому власником `LAB_REVIEW_TRUST_ROOT`. Sandbox не отримує цей trust store,
owner/GitHub token або writable control state. Тестові signed reviewer/stand responses
є контрольованими зовнішніми відповідями; вони **не кваліфікують physical ir**.

## Середовище та логи

Windows, Python 3.10.11; actual CORE/Worker subprocesses, portable Redis 8.10.3,
controlled HTTP providers, Playwright Chromium, готовий Angular build.
Sandbox: disposable Alpine 3.24.3 VM (kernel 6.18.58), QEMU TCG 11.1.0,
Docker Engine 29.9.2; API тільки через loopback host forward. Немає production mounts,
socket або Windows feature installation. VM вимкнена після перевірки.

| Перевірка | Результат | Лог у `.pytest_tmp/` |
|---|---|---|
| Спільний module/workflow/backup/resource/governance набір | 204 passed | `issue-369-370-regression.log` |
| Architecture/backup compatibility | 133 passed | `issue-369-370-compatibility.log` |
| Browser + governance/transfer/transport | 38 passed (включно з 2 Browser scenarios) | `issue-370-browser-recheck.log` |
| Actual sandbox build/run | 1 passed; усі 8 runtime checks true | `issue-370-sandbox-final.log`; `issue-370-sandbox-final/**/build.log`, `sandbox.log` |
| Budget/executor/CLI regression | 56 passed | `issue-370-budget-final.log` |
| Фінальний Lab executor/governance/CLI/API набір | 106 passed | `issue-370-final.log` |
| Перевірка підтвердженого remote close | 56 passed; додатковий negative close test 26 passed | `issue-370-close-final.log`, `issue-370-close-recheck.log` |
| Увесь фінальний цільовий набір; required PostgreSQL та Lab Browser | **590 passed**, 0 skipped, 2 dependency deprecation warnings | `issue-369-370-verified-scope.log` |
| Остаточна Compose isolation/config перевірка | 28 passed; обидва build contexts/Dockerfile paths коректні | `issue-370-compose-final.log` |

Повний діагностичний `pytest` (`issue-369-370-full.log`) зупинено після 8 failures
у загальному `tests/test_browser_e2e.py` (admin/viewer/video engine/tests run/character
сценарії); це **не green full suite**. Причини цих Browser failures цим звітом не
кваліфікуються; наявні сторонні зміни `tests/test_browser_e2e.py` збережені.
Дочірні процеси саме зупиненого запуску завершено; сторонній CORE не зупинявся.

Широкий цільовий набір (`issue-369-370-final-scope.log`): 588 passed, 1 failed.
Єдиний failure — actual PostgreSQL startup: temporary Unix socket path перевищив
107 bytes у Windows user temp. Без зміни тесту повторено з коротким
`--basetemp=.pytest_tmp/pg369`: 1 passed (`issue-369-postgres-recheck.log`).
Повтор усього цільового набору з `--basetemp=.pytest_tmp/sc369` і required
PostgreSQL/Browser: `issue-369-370-verified-scope.log`.
Результат повтору: **590 passed** за 144.36 s, без skips. Sandbox Compose використовує
той самий root build context та `network_mode: none`, що й фактична негативна
перевірка контейнера; resource URL/token передаються лише `lab-control`.

`compileall` та `git diff --check`: PASS. Артефакти temporary host збережені в
`issue-370-docker-host/`, без секретів.

## Межа завершення

Локальна реалізація критеріїв підготовлена для review. Публікація та acceptance на
новому immutable SHA ще не виконані: потрібне окреме доручення користувача на push.
Issues залишаються відкритими. Production switch/retirement та physical VRAM/VM
acceptance залишаються у чинних #326/#329; цей звіт їх не зараховує.
