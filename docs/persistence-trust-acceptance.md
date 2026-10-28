# Приймання #354 та #355

Локальне середовище: Windows, disposable Python processes, loopback HTTP,
synthetic signing/encryption keys і fixtures; production не змінюється.
Повний Vertep SHA, dirty/clean статус та hashes тестів фіксує JUnit fixture
`acceptance_provenance`. Dirty результати не є green exact-SHA CI для main.

| Критерій | Test IDs / реалізація | Підтверджений обсяг та межі |
|---|---|---|
| #354 AC1 | `test_apply_deployment_docker::test_standard_apply_rescues_before_actual_recreate_and_consumer_reads_back`; `test_apply_deployment.py` | Новий Docker test викликає штатний `apply()` без підміни runner: legacy-only files → rescue → Compose recreate → mounted consumer read-back з artifact hash; повторний apply зберігає новіші дані. Локально BLOCKED без Docker. Готові failure/non-overwrite unit regressions збережені. |
| #354 AC2 | `test_backup_process_acceptance::test_real_restore_confirmation_restart_and_tamper` | Справжній Backup Service process: encrypted snapshot, snapshot-bound confirmation, process restart до confirm, actual file restore, повторний restart/progress та відмова використаному token; tamper відхилено до mutation. Standalone позитивний file path не є доказом healthy CORE. |
| #354 AC2 | `test_crash_and_competing_process_keep_restore_fence` | Справжні competing service processes і actual file restore; зовнішній DB command контрольований лише як barrier. Навіть expired TTL не краде live guard. Crash → restart → persisted failure та unconfirmed EMERGENCY без CORE; fence збережено. DB restore цим не підтверджується. |
| #354 AC2 | `test_backup_restore_durable::test_confirmation_is_rechecked_after_waiting_for_actual_guard`, `test_restore_cannot_replay_historical_normal_over_live_restore_fence` | Timing fault навколо справжнього guard не дозволяє stale confirmation виконати другу mutation; historical NORMAL зі snapshot не стирає live restore fence. Це regressions на додаток до process proof. |
| #354 AC2–3 | `test_core_caller_fences_writers_and_unhealthy_restore_cannot_enter_normal` | Справжні CORE та Backup Service processes, internal authentication, CORE caller confirmation/execute/progress. HTTP writers блокуються restore fence. Реальна unhealthy відповідь CORE дає persisted failure і підтверджений EMERGENCY, збережений після restart CORE. Healthy connected success та повний Web/Telegram positive flow ще потребують відповідного стенда. |
| #354 AC4 | `test_approved_media_consumer_readback_after_actual_restore_and_restart` | Чинний media harness створює Native approved/published output. Actual encrypted backup → mutation → restore → restart Backup Service → новий CORE process/HTTP consumer: version history, storyboards/approvals, receipts/mappings і file hashes збігаються. Generator transports залишаються погодженими fixtures. Pinned MPT та штатний Linux update/recreate/rollback — BLOCKED без Docker/Ubuntu; не підміняються довільними JSON. |
| #354 AC5 | `test_full_media_harness.py`, `test_media_acceptance_report.py` | Clean run використовує один UUID run ID; report звіряє незалежний `--expected-sha`. Existing mixed-run validator збережено. `test_same_sha_different_run_ids_remain_rejected` та `test_uniform_substituted_sha_fails_independent_expected_identity` відхиляють змішані runs і однотипно підмінений SHA. CI paths розділено за run ID/attempt без ігнорування записів. |
| #355 AC1 | `test_real_tests::TestIssue138` | Чинний built-runtime identity positive proof повторно не реалізовано. Трасування negative tests: `test_missing_identity_fails_closed_without_git`, `test_unknown_baked_identity_blocks_acceptance`, `test_truncated_identity_blocks_acceptance`, `test_mismatched_identity_blocks_acceptance`. |
| #355 AC2 | `test_release_qualification::test_actual_bundle_builder_to_qualification_fail_closed` та чинні qualification/contract tests | Справжній runtime bundle builder → artifact-root qualification, synthetic signing key, незалежний full SHA. Missing runtime, substituted SBOM/images inventory/checksums відхиляються. SHA256SUMS тепер перераховується та має покривати signed files і manifest. Image lock у цьому локальному тесті — fixture; actual images build не підтверджено без Docker. |
| #355 AC3 | `test_trust_caller_acceptance::test_pki_enrollment_renew_expiry_revoke_at_worker_consumer`, `test_node_registry.py`, `test_security_check.py`, `test_key_lifecycle.py` | Actual registration/CSR/renew/revoke APIs та Worker heartbeat credential consumer: wrong identity/expired registry credential/old renewed credential/revoked credential → 401. OpenSSL перевіряє CRL revocation. `/api/security/check` бачить missing node CA, штатне enrollment provision усуває саме цей дефект. Чинні SAN, crypto та sealed-store regressions повторно використані. Це не заміна actual TLS proxy handshake/HTTPS reload. |
| #355 AC4 | `test_safe_update.py`, `test_update_security.py`, `test_apply_deployment.py`, чинні bootstrap tests | Готові regressions збережено; вони не оголошуються повним Ubuntu bootstrap executor proof. Ubuntu 24.04 containers/preflight/HTTPS renewal/reload/persistence/rerun, amd64/arm64 та повний install/update/recovery залишаються BLOCKED без disposable Ubuntu/Docker стенда. |

Основні результати зберігаються в `.pytest_tmp/issues-354-355-final.xml`,
`.pytest_tmp/issue-354-process.xml` та окремому media evidence/report каталозі
конкретного запуску. Фактичні PASS/FAIL/skip беруться з цих запусків; таблиця
не перетворює skip або підготовлений Docker test на PASS.

CI `local-contracts` вимагає OpenSSL до pytest і зберігає JUnit/process logs;
media job перевіряє expected full SHA. Непідтверджений EMERGENCY залишається
явним failure. Після crash fence не прибирається через очищення Python cache
або прострочення TTL активної/перерваної операції.

Штатний CORE caller переводить drained installation у MAINTENANCE і встановлює
snapshot-bound restore fence до видачі confirmation. Active jobs/inflight leases
та інший pending restore блокують початок. Web policy допускає restore у
MAINTENANCE відповідно до API; legacy `RECOVERY` приведено до глобального
стану `RECOVERING`. Backup Service використовує чинний internal API key;
нормалізація стану вимагає actual CORE health. Pending/error не видаються за
успішне відновлення або автоматичний NORMAL.
