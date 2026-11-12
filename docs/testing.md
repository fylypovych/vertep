# Тестування

> Статус: чинний | Аудиторія: розробник | Канон: [AGENTS.md](../AGENTS.md) §2.9, §2.12, §11.

## Перевірки за командою власника

Нормативна послідовність — [AGENTS.md §2.9](../AGENTS.md#29-перевірки-перед-commit-і-push); документація не надає дозволу виконувати push/release.

| Команда | Локальні перевірки | Публікація |
| --- | --- | --- |
| `пуш` | `python -m compileall -q core adapters worker scripts installer tests`, `git diff --check`; без pytest, Playwright і вибіркових тестів | Версійний commit із `[skip ci]`, push у main; без tag/Release |
| `тест` | Ті самі швидкі перевірки та `python -m pytest -q -n 8 --dist loadgroup` | Commit без `[skip ci]`, push; CI і Browser E2E |
| `реліз` із локальними змінами | Повна логіка `тест`, далі release workflow | Лише після перевірок точного SHA |
| `реліз` із чистим деревом | Без нового commit; перевірка CI/Browser gate точного SHA, release workflow виконує свої тести | Tag і Release створює лише workflow |

Перевіряються відсутність нових падінь, секретів/ключів у diff та узгодженість `VERSION = commit = CHANGELOG = releases/<версія>.md`. Для локального виконання Issue запускаються доречні тести його scope; це не команда `тест` і не дозвіл на публікацію.

## Рівні

- **Unit/contract/integration** (`tests/`) — локально, без живого сервера: API, tasks, queue semantics, worker contracts, control-plane межі, backup/restore на disposable-сховищах.
- **Browser E2E** (Playwright, `web-v2/tests`, `tests/test_browser_e2e.py`) — критичні flows зі справжнім локальним backend і реальними тестовими артефактами; route-mock тести валідні лише для UI, не для backend-контрактів.
- **Qualification-скрипти** — `scripts/qualify-release.py` (артефакти релізу), `scripts/qualify_infrastructure.py` (S01–S08, JSON-доказ).
- **Real Test** (`ir` Issues) — фактичний стенд: clean Ubuntu, multi-host, physical GPU, live platforms; mock/skipped/green unit — не доказ.

## Infrastructure qualification (`scripts/qualify_infrastructure.py`)

- Без прапорців — статичний `PLAN`-каталог, `overall=PLAN`, код 1 (`--plan-only` дозволяє код 0).
- `--run` — виконує automated pytest-цілі сценаріїв: `PASS` лише коли **всі** пункти `PASS`; `FAIL`/`NO_TESTS` (порожній або повністю пропущений прогін) — `FAIL`; лише `PROCEDURE`/`NOT_RUN` — `PARTIAL`, код 1 (`--allow-procedures` піднімає такий прогін до `PASS`).
- Код виходу 0 лише для `overall=PASS` (або `PLAN` з `--plan-only`); `--selected S99` чи `rt_issue` поза Issues з частковим чи повним покриттям у `IR_COVERAGE` (зараз `{42, 47, 54, 441, 442}`) — код 1.
- Кожен `rt_scenario` прив'язаний лише до свого `rt_issue`; сценарійні реальні процедури (bootstrap, backup roundtrip, міграція, перерваний update, trust релізу, publisher receipt) не підміняються generic health-пробами.
- Каталог «Реальні тести» локальний (`SCENARIOS` у `scripts/qualify_infrastructure.py`), а не список GitHub Issues: 8 сценаріїв S01–S08 перевіряють окремі критерії; один Issue може мати кілька сценаріїв (#442 — S01, S04, S05, S06). `IR_COVERAGE` описує кожен відкритий real-server Issue: `partial` — сценарії перевіряють лише критерії з `covers`, `none` — виконуваного сценарію немає (#63, #326, #329, #420), `full` — усі критерії Issue мають mandatory checks. GitHub Runner публікує результат у Issue, але автоматично закриває його тільки при `full`; зараз жоден Issue не має повного покриття. Під час зміни відкритих `ir` або сценаріїв реєстр оновлюється разом із каталогом (#451).

## Redis queue contract (`tests/test_queue_redis_contract.py`)

- Реальні контракти Redis-бекенду `TaskQueue`: claim/ack/renew/requeue/dead-letter/cancel/generation/watchdog lock, спільний стан між двома клієнтами.
- Джерело Redis: `VERTEP_TEST_REDIS_URL` (CI-сервіс) → локальний `redis-server` → явний `skip`; недоступний `VERTEP_TEST_REDIS_URL` — `fail`.
- Персистентність: AOF і RDB переживають restart, витерта директорія стартує порожньою; контракт `appendonly yes` перевіряється для `docker-compose.yml` і `deploy/docker-compose.yml`.
- Весь решта suite працює виключно на local-бекенді (`conftest.py` фіксує недоступний `REDIS_URL`).

## Політика

- Нові падіння модульних тестів блокують реліз; допустимі e2e-невдачі — лише відсутність запущеного сервера (`ERR_CONNECTION_REFUSED 127.0.0.1:8080`).
- Skipped ≠ passed: пропущені перевірки (OpenSSL, GPU, live) фіксуються явно і не зараховуються.
- Release gate: required Browser E2E для точного release SHA має бути green — інакше публікація блокується ([AGENTS.md §2.12](../AGENTS.md)).

## CI

- `ci.yml` — повний suite, компіляція, shell/compose-перевірки для подій push/PR без пропуску CI; redis-сервіс + `redis-server` для queue contract; окремий job `infrastructure-qualification` (`--run`) з артефактом `infrastructure-qualification.json`.
- `browser-e2e.yml` — Browser suite з артефактами (logs/traces).
- `release.yml` — build images, runtime bundle, manifest/checksums/SBOM/підпис, валідація, tag + GitHub Release. Єдиний власник публікації.
