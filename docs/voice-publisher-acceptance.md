# Voice / Publisher acceptance — #360, #361

Локальна база: `a98e2de4a19a6906c9403e8495a37275baa8f445`, `0.0.2.87`.
Результат перевіряється з локальними змінами; це не green CI майбутнього published SHA.
JUnit містить `source_sha`, `tracked_diff_sha256`, hash тесту та стан робочого дерева.

## #360 — i.0.0.3.60

| AC | Реалізація | Test ID / expected |
| --- | --- | --- |
| AC1 / #165.1 | `services/tts_service.py`: каталог stock espeak-ng, supported provider/model/language/voice/speed | `test_voice_http_acceptance::test_actual_espeak_catalog_parameters_and_measurements`: справжній WAV, 120 WPM довше за 240 WPM, інша voice/language дає інший hash; unsupported → 422 |
| AC2 / #165.2 | `core/api/tasks.py`: issued snapshot та чинний owner/lease до file replacement, registration, ack | `test_voice_http_acceptance::test_issued_voice_result_before_mutation`: missing/mismatch/snapshot/foreign/expired → 400/409 без зміни файлів, artifacts та owner; live config drift не підміняє issued task |
| AC3 / #165.3 | Voice loss/lease/restart recovery, збереження TTS stage; штатна assembly | `test_delivery_process_acceptance::test_actual_voice_audio_assembly_recovery`: normal/loss/reconnect/lease/restart → accepted video з декодованим audio stream; cancel/stale/duplicate → 409; hash audio збігається з Worker contract |

espeak-ng виконується в окремому TTS HTTP процесі. Локально використано portable
espeak-ng 1.52.0; version та binary SHA256 записуються в JUnit. CORE і Redis —
окремі процеси. Ollama HTTP та зовнішні GPU images контрольовані; самі queue,
claim/result, watchdog, persistence, espeak та FFmpeg не підміняються в процесному
контурі. HTTP boundary-тести додатково використовують чинний controlled upstream
fixture, не замінюючи result mutation. Physical quality лишається в #34.

## #361 — i.0.0.3.61

| AC | Реалізація | Test ID / expected |
| --- | --- | --- |
| AC1 / #166.1 | Worker publication ledger: fsync до upload; persisted receipt до CORE ack; runtime identity reconciliation; CORE restart не скасовує живий lease | `test_delivery_process_acceptance::test_publisher_process_private_delivery_and_lost_ack_restart` (5 платформ): нові Worker процеси не викликають HTTP upload повторно; `test_core_restart_keeps_live_publisher_lease_and_accepts_its_result`: живий owner зберігається |
| AC1 / ambiguous response | Durable `UPLOADING` + `outcome_unknown`, збережений intent | `test_lost_upload_response_is_durable_and_never_blindly_uploaded_again`: transport отримав один upload, response втрачено; після restart — нуль повторних upload, unknown не видається за PUBLISHED |
| AC2 / #166.2 | Receipt job/task/channel/version/hash та active owner/lease перевіряються до cleanup/ack | `test_publisher_http_boundary::test_actual_publish_result_preserves_owner_until_valid`: missing/stale/foreign/mismatch → 400/409, accepted state та owner не змінюються |
| AC3 / #166.3 | Retryable HTTP failures, реальний queue not_before, bounded exhaustion; permanent / NOT_CONFIGURED без retry | `test_actual_claim_time_backoff_exhaustion_and_permanent_failure`: ранній claim → None, claim після delay, exhaustion → FAILED; `test_partial_publish_retry_preserves_successful_channel_across_restart`: retry лише невдалого каналу |
| AC4 / #166.4–5 | Approved input HTTP delivery до приватного Worker filesystem, hash/size/version до adapter upload; збереження platform parameters через storyboard approval | `test_publisher_process_private_delivery_and_lost_ack_restart`: 5 adapters через approved video → queue → claim → Worker process → adapter → result; foreign access/corrupt input/wrong size/version не викликають upload |
| AC5 / #167 | Encrypted credential expiry/scopes/revoke/reconnect state; усі п'ять credential families читають Worker secret store; штатні required IDs перевіряються до upload | `test_publisher_oauth_state`: actual controlled OAuth HTTP refresh та expiry; fresh provider instances читають persisted state; revoked/expired/missing scopes блокують upload |
| AC5 / Browser | Settings test/reconnect використовують фактичні API та показують ready/missing scopes/error | `test_publisher_oauth_browser_e2e::test_publisher_test_reconnect_scope_error_and_secret_redaction`: Browser DOM відповідає API, raw credentials відсутні |

Для Publisher підміняється тільки зовнішній platform HTTP transport. CORE,
Redis, Worker subprocesses, filesystem delivery та ledger працюють штатно.
При невідомому remote outcome система не вгадує remote_id і не робить сліпий
повтор: intent/unknown зберігаються для reconciliation. Успішний відомий receipt
відновлюється автоматично. Live accounts лишаються в #47.

## Відтворення

Процесні тести: `python -m pytest -q tests/test_delivery_process_acceptance.py tests/test_voice_http_acceptance.py -o junit_family=legacy --junitxml=processes.xml`.
Потрібні справжні `redis-server`, `espeak-ng` і FFmpeg (або imageio-ffmpeg).
`VERTEP_REQUIRE_TEXT_REDIS_PROCESS=1` та `VERTEP_REQUIRE_ESPEAK=1` забороняють
зарахування відсутнього runtime через skip. Portable espeak використовує
`ESPEAK_EXECUTABLE` та `ESPEAK_DATA_PATH`.

Browser: production build `web-v2`, потім `VERTEP_REQUIRE_MEDIA_BROWSER_E2E=1`
і `python -m pytest -q tests/test_publisher_oauth_browser_e2e.py`.
CI запускає mandatory runtime тести в `media-acceptance`, Browser — у
`browser-e2e`; публікація коду чи запуск Release цим не дозволяються.

Загальна регресія має окремий baseline failure:
`tests/test_real_tests.py::test_backup_browser_telegram_rbac`. Він відтворюється
в ізольованому `git archive a98e2de4a19a6906c9403e8495a37275baa8f445`:
тест шукає literal `admin` / `role` / `auth` у `services/backup_service.py`.
Файл і цей тест не змінювалися в #360/#361; результат не зараховується як failure
Voice/Publisher і не приховується вимкненням тесту.

Локальний результат: усі **44** acceptance cases п'яти нових test modules — PASS,
без skip; окремі OAuth/no-secrets Browser перевірки — **2 passed**.
Повний запуск: **2997 passed, 4 failed, 136 skipped**. Два старі Telegram fixtures
доповнено issued job/task identity без послаблення assertions; усунено зайве
читання secret при cached OAuth access. Повторна перевірка цих трьох failures
разом із Publisher/OAuth регресіями — **91 passed**. Залишається лише наведений
вище baseline failure. JUnit: `.pytest_tmp/issue-360-361-confirmed/all.xml`,
`corrections.xml`, `oauth.xml`; Browser — `.pytest_tmp/issue-360-361-final/browser.xml`.
