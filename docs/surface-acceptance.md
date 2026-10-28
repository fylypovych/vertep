# Автоматизоване приймання #356 та #357

Тести використовують disposable CORE, production Angular build і Chromium.
Нові secret formats або surfaces до погодженого corpus не додаються.

| Критерій | Перевірка | Очікуваний результат |
|---|---|---|
| #356 AC1, AC3 | `test_no_secrets_browser_e2e.py::test_corpus_http_persistence_and_dom` | Чинний `SECRET_CORPUS` проходить `/api/tasks/result`; HTTP response, persisted metadata/receipt, повторне HTTP читання і DOM не містять значень secrets; benign note/channel/status збережені. |
| #356 AC2 | `test_no_secrets_acceptance.py::test_secret_corpus_never_crosses_real_surfaces` | Той самий corpus перевірено в logs, API logs, Telegram transport, Runner report, audit та error response. |
| #357 AC1 | `test_monitoring_scrape.py::test_actual_targets_series_rules_and_dashboard` | Справжні Prometheus, Loki, node-exporter і CORE: чотири targets up, незмінні scheme/ports/labels; actual series та виконання чинних PromQL rules/dashboard queries. |
| #357 AC2 | `test_monitoring_browser_e2e.py::test_probe_failure_ack_recovery_survives_process_restart` | Зупинка heartbeat service process → штатний stale-worker probe → entity alert → Browser ack → restart CORE process → recovery service → resolved → повторний restart CORE. ID та стани збережені. |
| #357 AC3 | `test_monitoring_browser_e2e.py` | Actual HTTP ingestion 205 logs → cursor pagination 200+5 → node navigation; empty, затриманий справжній HTTP для loading, network outage для error. |
| #357 AC3, готові регресії | `test_monitoring_api.py`, `test_monitoring.py` | Retention, dedup, watchdog invalid body/history, required readiness та чинні API/config contracts. |

Cursor містить timestamp і стабільний ідентифікатор redacted запису з ordinal
для однакових записів. Це зберігає записи з однаковим timestamp на межі
сторінок; старий `before=<ISO timestamp>` залишається сумісним. HTTP regression
`test_log_cursor_preserves_timestamp_ties_and_identical_records` перевіряє
вісім однакових записів у чинному та rotated файлах без втрат і повторів.

Запуск після `npm run build` у `web-v2/` та встановлення Chromium:

```powershell
$env:VERTEP_REQUIRE_MEDIA_BROWSER_E2E = "1"
$env:VERTEP_REQUIRE_MONITORING_SCRAPE = "1"
python -m pytest -q tests/test_no_secrets_browser_e2e.py tests/test_no_secrets_acceptance.py tests/test_monitoring_browser_e2e.py tests/test_monitoring_scrape.py tests/test_monitoring_api.py tests/test_monitoring.py -o junit_family=legacy --junitxml=.pytest_tmp/surface-acceptance.xml
```

Scrape потребує Docker Linux engine; відсутність engine з увімкненим required
flag є FAIL, без flag — skip/BLOCKED, а не підтверджене приймання.
Маскування probe, ручна вставка alert або mock metrics не використовуються.

Browser E2E workflow зберігає JUnit, actual API/scrape evidence, CORE process
logs та screenshots в artifact `surface-acceptance-<SHA>`. JUnit містить повний
`source_sha`, статус clean/dirty, SHA256 tracked diff і тестового файла.
Локальні dirty результати не є доказом незміненого commit або публікації в main.
Фактичні PASS/FAIL/skip та середовище беруться з конкретного запуску JUnit;
ця таблиця визначає трасування й не оголошує непроведений scrape успішним.
