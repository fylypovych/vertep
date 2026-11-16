# Аудит Vertep — 15.11.2026

## Актуальна звірка #488 — 16.11.2026, main 0.0.3.80

**P3 backup-команди: PARTIAL; попередня позначка «виконано локально» нижче не є повним прийманням.** HEAD = remote main `f86cc786b93eff741941fae8c919771fa04f23e5`; [реалізація 0.0.3.80](https://github.com/fylypovych/vertep/commit/f86cc786b93eff741941fae8c919771fa04f23e5) опублікована. Сторонню незакомічену зміну `tests/test_node_process_acceptance.py` збережено; цей файл не входив у прогін.

- **Підтверджено:** `services/backup_service.py::_pg_dump_default`, `_pg_restore_default`, `_run_command` запускають built-in argv без shell; Redis password передається через `REDISCLI_AUTH`, PostgreSQL успадковує `PGPASSWORD`; `_remote_copy` екранує snapshot path. Операторські shell overrides дозволені критеріями й описані в `docs/backup-restore.md`; hard errors для ненульових кодів/відсутніх dumps збережені.
- **Залишок:** `_redis_dump_default` губить TLS scheme і ACL username з `REDIS_URL`. Для синтетичного `rediss://backup_user:synthetic_password@cache.example:6380/2` отримано `['redis-cli', '-h', 'cache.example', '-p', '6380', '-n', '2', 'BGSAVE']`: немає `--tls` і `--user`. Старий виклик через `-u` передавав ці дані. Це регресія перетворення конфігурації, а не нова функціональність. Наявність такого URL в production не встановлена.
- **GitHub:** [#488 · i.0.0.4.88](https://github.com/fylypovych/vertep/issues/488) закрито **NOT_PLANNED** після перевіреного перенесення залишку до [#490 · i.0.0.4.90](https://github.com/fylypovych/vertep/issues/490), **OPEN**. Виконані частини повторно не призначені. Новий ir не створено: залишок перевіряється кодом; live backup/restore лишається окремим стендовим прийманням.
- **Тести:** `test_backup_commands_488.py`, `test_backup_restore.py`, `test_backup_restore_durable.py`, `test_backup_read_only.py`, `test_access_and_backup_defaults.py`, `test_backup_confirmation_progress.py`, `test_backup_module_state.py` — **66 passed, 1 skipped, 11 warnings**, 2.60 с. Skip: `VERTEP_TEST_REDIS_URL` не заданий. Журнал: `/tmp/vertep-488-review.log`. Окреме безмережеве відтворення втрати TLS/username підтверджене; live PostgreSQL/Redis не запускались. Зелені тести не покривають знайдені URL cases.
- Node-exporter залишається прийнятим власником ризиком поза scope. Змінено тільки цей звіт та GitHub Issues; без змін коду, commit, push або release.

---

## Актуальна звірка #484–486 — 16.11.2026, main 0.0.3.79

Цей розділ замінює попередні OPEN/FAIL/PARTIAL висновки щодо A4/#484 та A7/#485; старі записи нижче збережено як історію. До перевірки робоче дерево чисте, HEAD = remote main `40235dfa645dabcecfc5380698cec4f226175517`. Прочитано актуальні описи й коментарі #484–486, перевірено код, commit 0.0.3.78–0.0.3.79 і наявні #42/#47/#420/#487 для уникнення дублювання.

| Issue | Результат і докази |
|---|---|
| [#484 · i.0.0.4.84](https://github.com/fylypovych/vertep/issues/484) | COMPLETED. A4: `core/security.py::_valid_worker_token` дозволяє shared fallback тільки без mTLS. Власні tokens/hashes, certificate identity і revocation збережено. `tests/test_mtls_shared_token.py` відтворює первісний дефект і перевіряє позитивні/негативні випадки; `test_worker_routes_auth.py` підтверджує routes. Реалізація — [0.0.3.78](https://github.com/fylypovych/vertep/commit/804a9451a06fdb92d54a6da69e4f3f6c58170464). |
| [#485 · i.0.0.4.85](https://github.com/fylypovych/vertep/issues/485) | COMPLETED. A7: `core/api/jobs.py::create_job` та `core/telegram/job_callbacks.py::_create_job_from_telegram` відхиляють incomplete explicit-ID персонажа для нового job; історичне читання збережено. У поточному каталозі немає `char-contract`, усі shipped characters повні. Перевірено completeness/default/isolated voice issued-result regressions. Реалізація — 0.0.3.78–0.0.3.79. |
| [#486 · var.0.0.4.86](https://github.com/fylypovych/vertep/issues/486) | PARTIAL → CLOSED/NOT_PLANNED після перенесення. Кодове розділення підтверджено в [0.0.3.79](https://github.com/fylypovych/vertep/commit/40235dfa645dabcecfc5380698cec4f226175517): окремий publisher secret, task scope без intake token, одна реалізація `publishers/telegram`, brand target, без глобальної міграції, channel readiness і Web UI credential. Live-публікація та фактичні права двох ботів НЕ підтверджені; залишок — [#489 · ir.0.0.4.89](https://github.com/fylypovych/vertep/issues/489), OPEN. |

#486 — додаткове рішення власника після первісного аудиту, а не новий дефект A1–A9. Реальна перевірка у #489 переносить тільки його невиконаний критерій двох ботів. Загальні Publisher/live-platform сценарії залишаються #47, polling/operations — #42; однакові докази повторно не вимагати. Модульне пакування Telegram та Application Modules залишаються в #487; цей аудит не оголошує їх виконаними. Попереднє рішення тримати real acceptance всередині VAR замінено поточним дорученням користувача перенести залишок у i/ir.

### Перевірки та межі

- **108 passed**, без skips: `test_mtls_shared_token.py`, `test_worker_routes_auth.py`, `test_character_completeness.py`, `test_default_character.py`, `test_voice_issued_result.py`, `test_telegram_split_486.py`, `test_issue_411_publisher_channels.py`, `test_setup_starts_telegram_polling.py`.
- **23 passed, 27 deselected**: `test_issues_427_429.py -k 'telegram or node_token or core_readiness or every_brand_channel'`. Перевірено readiness з правами/без прав, missing target і кількома каналами брендів. Deselected не зараховано як PASS.
- Разом **131 passed**; журнали цього запуску: `/tmp/vertep-audit-1611-tests.log`, `/tmp/vertep-audit-1611-readiness.log`. Попередні заяви автора про повний suite/215 Browser tests не зараховувались як власний повторний прогін.
- Повний suite, Browser E2E, production, live Telegram, Dell R720/VMware/GPU не перевірялися. Відсутність live доказів не означає доведений дефект коду. Реальна PKI лишається #54; #469 і решта стендових критеріїв не закриті цим аудитом.
- За прямим дорученням #484/#485 закриті completed; #486 — not_planned з посиланням на наступника. Історичні #473/#476 не змінювались. Код, VERSION, CHANGELOG, branches/commits/releases не змінювалися; звіт лишається локальним, без push.

---

## Актуальне приймання після публікації — main 0.0.3.77, 15.11.2026

**Цей розділ замінює попередній висновок про незакомічені виправлення та OPEN для 12 локально підтверджених Issues.** Звірено актуальні описи й коментарі #469–#483 та наступників #484/#485. До початку звірки робоче дерево чисте; local HEAD = remote main = `057c84f8f50016e54cecffaaa9a2bb1160113e29` (0.0.3.77).

Реалізація ввійшла до [commit 0.0.3.74](https://github.com/fylypovych/vertep/commit/4a5c9b051b115e6e6c367cb1bae1ad3a3e983063); його входження в поточний main перевірено. Перевірені core/worker/lab/adapters/deploy зміни від 0.0.3.74 збережені. Умова §4.5 про публікацію реалізації тепер виконана.

| Issues / ідентифікатори | Підсумок |
|---|---|
| #470–#472 / i.0.0.4.70–72 | COMPLETED: TTS dispatch, generation gate, proxy identity |
| #474–#475 / i.0.0.4.74–75 | COMPLETED: script budget/cancel/lease, configured default character |
| #477–#483 / i.0.0.4.77–83 | COMPLETED: off-loop I/O, historical notes, LLM readiness, script validation, LAB context, security hardening, test isolation |
| #473 / i.0.0.4.73 | Залишається CLOSED / NOT_PLANNED; невиконане — [#484 / i.0.0.4.84](https://github.com/fylypovych/vertep/issues/484), OPEN |
| #476 / i.0.0.4.76 | Залишається CLOSED / NOT_PLANNED; невиконане — [#485 / i.0.0.4.85](https://github.com/fylypovych/vertep/issues/485), OPEN |
| #469 / i.0.0.4.69 | BLOCKED / OPEN: VM-передумова не підтверджена, наскрізний цикл не реалізовано |

Нових наступників не створено: фактичні залишки вже повністю обліковані в #484/#485, а тимчасове відкладення #469 не потребує дубліката. Закриті раніше #473/#476 не змінювалися. Реальні ir #329/#63/#54 та інші залишаються незалежними від automated приймання цих i.

### Повторні докази

- Той самий набір 15 цільових файлів: **239 passed, 5 skipped**, 31.95 с; JUnit `/tmp/vertep-review-377.xml`, log `/tmp/vertep-review-377.log`.
- Усі 5 skips — proxy без nginx у PATH. Явний nginx 1.24, `VERTEP_REQUIRE_PROXY_ACCEPTANCE=1`: **6 passed, 0 skipped**, 22.79 с; `/tmp/vertep-review-377-proxy.log`. Разом **244 унікальні PASS**; один статичний proxy case виконано двічі.
- Розподіл результатів за критеріями збережено в таблиці попередньої звірки та нових підсумкових GitHub-коментарях. Вони тепер застосовуються до опублікованого SHA, а не dirty working tree.
- [Browser E2E поточного SHA](https://github.com/fylypovych/vertep/actions/runs/43747512439): success. [CI](https://github.com/fylypovych/vertep/actions/runs/43747528987): in_progress на момент читання; це не green CI. Повний suite/Browser локально не повторювали, production/physical перевірки не виконували.
- `git diff --check`: PASS. Критерії цих i підтверджені цільовими автоматизованими тестами; нових вимог загального CI, Release або фізичного стенда не додавали.

За прямим дорученням користувача 12 виконаних Issues закрито completed із підсумковими коментарями та commit-посиланнями. Зміни цього проходу: лише цей звіт і GitHub-коментарі/статуси; код, VERSION, CHANGELOG, Git history, push/release не змінювалися. Попередні розділи нижче збережено як історію перевірок.

---


## Актуальна незалежна звірка #469–#483 — 15.11.2026

**Цей розділ має пріоритет над попередніми позначками IMPLEMENTED та історичними висновками нижче.** Перевірено актуальні описи/коментарі всіх 15 Issues через автентифікований GitHub CLI, поточний код і цільові тести. Local HEAD та remote main: `1af8f9cd54090bef0408dff86627ff661bf59af9`, версія 0.0.3.73. Виправлення #470–#483 лишаються незакоміченими; посилання на базовий SHA не є посиланням на commit їх реалізації.

### Вердикт

- 12 Issues локально підтверджені: #470–#472, #474–#475, #477–#483. Вони **OPEN**, бо §4.5 AGENTS.md вимагає потрапляння реалізації в main перед completed. Дозвіл на аудит/закриття не дає права на push; нового дубліката лише для публікації не створено.
- #473 — фактичний невиконаний scope перенесено до [#484, i.0.0.4.84](https://github.com/fylypovych/vertep/issues/484).
- #476 — тільки невиконаний залишок перенесено до [#485, i.0.0.4.85](https://github.com/fylypovych/vertep/issues/485); готові локальні guards не призначено повторно.
- #473 і #476 закриваються **not_planned**, не completed, після перевірки повноти наступників (§4.5). Це не успішне приймання.
- #469 лишається **BLOCKED / OPEN** за чинною передумовою VM. Тимчасове блокування не є підставою дублювати цей Issue чи створювати новий ir. Реальний scope залишається #329/#63; інші стендові ir не змінювались.

| Issue | Аудит | Результат / GitHub | Доказ або залишок |
|---|---|---|---|
| [#469](https://github.com/fylypovych/vertep/issues/469) (`i.0.0.4.69`) | F3 | BLOCKED; OPEN | Передумова VM не підтверджена; не реалізовано. Без дублювання #329/#63. |
| [#470](https://github.com/fylypovych/vertep/issues/470) (`i.0.0.4.70`) | A1 | LOCAL VERIFIED; OPEN | Worker dispatch + preview: 14 + 24 PASS; контрольований HTTP TTS, не physical multi-host. |
| [#471](https://github.com/fylypovych/vertep/issues/471) (`i.0.0.4.71`) | A2 | LOCAL VERIFIED; OPEN | Generation gate: 44 PASS; межі статичного аналізу задокументовані. |
| [#472](https://github.com/fylypovych/vertep/issues/472) (`i.0.0.4.72`) | A3 | LOCAL VERIFIED; OPEN | nginx 1.24: 6 PASS; worker route auth: 12 PASS. Production PKI — #54. |
| [#473](https://github.com/fylypovych/vertep/issues/473) (`i.0.0.4.73`) | A4 | FAIL; NOT_PLANNED → #484 | mTLS=true зі спільним token дає True без індивідуального credential; перенесено до i.0.0.4.84. |
| [#474](https://github.com/fylypovych/vertep/issues/474) (`i.0.0.4.74`) | A5 | LOCAL VERIFIED; OPEN | Budget/cancel/lease: 13 PASS; in-flight HTTP має bounded timeout, не миттєве переривання. |
| [#475](https://github.com/fylypovych/vertep/issues/475) (`i.0.0.4.75`) | A6 | LOCAL VERIFIED; OPEN | Default character: 20 PASS; configured selection та явна відмова без default. |
| [#476](https://github.com/fylypovych/vertep/issues/476) (`i.0.0.4.76`) | A7 | PARTIAL; NOT_PLANNED → #485 | 13 PASS не покривають permissive explicit-ID readiness. Залишок — i.0.0.4.85. |
| [#477](https://github.com/fylypovych/vertep/issues/477) (`i.0.0.4.77`) | A8 | LOCAL VERIFIED; OPEN | Hash/upload/storage off-loop: 13 PASS; integrity/containment збережені. |
| [#478](https://github.com/fylypovych/vertep/issues/478) (`i.0.0.4.78`) | A9 | LOCAL VERIFIED; OPEN | Release notes: 3 PASS, фактичний git show звірено; файл ще untracked. |
| [#479](https://github.com/fylypovych/vertep/issues/479) (`i.0.0.4.79`) | P3 LLM | LOCAL VERIFIED; OPEN | Readiness/config/Worker: 15 PASS. |
| [#480](https://github.com/fylypovych/vertep/issues/480) (`i.0.0.4.80`) | P3 script | LOCAL VERIFIED; OPEN | Structure metadata/duration/brief: 18 PASS. |
| [#481](https://github.com/fylypovych/vertep/issues/481) (`i.0.0.4.81`) | P3 LAB | LOCAL VERIFIED; OPEN | Context limits/empty content/report_only: 13 PASS. |
| [#482](https://github.com/fylypovych/vertep/issues/482) (`i.0.0.4.82`) | P3 security | LOCAL VERIFIED; OPEN | Cookie/key permissions/setup rate limit: 12 PASS. |
| [#483](https://github.com/fylypovych/vertep/issues/483) (`i.0.0.4.83`) | P3 тести | LOCAL VERIFIED; OPEN | Worker loop fixture isolation: 24 PASS. |

### Нові перевірки цієї звірки

- `.venv/bin/python -m pytest -q` для 15 цільових файлів (voice dispatch/preview, generation gate, proxy/auth, budget, default/completeness, system I/O, notes, LLM readiness, structure, LAB context, security, Worker isolation): **239 passed, 5 skipped**, 35.98 с. JUnit: `/tmp/vertep-review-469-483.xml`, log: `/tmp/vertep-review-469-483.log` (тимчасові локальні артефакти, не довгострокові GitHub-докази).
- П'ять proxy skips викликані відсутністю nginx у PATH. Повтор `tests/test_proxy_client_identity.py` з явним `VERTEP_NGINX_EXECUTABLE` і `VERTEP_REQUIRE_PROXY_ACCEPTANCE=1`: **6 passed, 0 skipped**, 19.28 с. Log: `/tmp/vertep-review-proxy-472.log`. Разом **244 унікальні cases PASS**, один статичний proxy case повторено; не 245 унікальних тестів.
- Контрольований експеримент #473: synthetic token, mTLS=true, valid certificate stub, individual verification=False, порожня node map → shared token прийнято. Це code-level доказ дефекту, не експлуатація production.
- Контрольований експеримент #476: `missing_character_files` повертає visual/generation/publishing; `load_character(..., "char-contract")` успішний, enabled=True. Видалення не є єдиним рішенням: допустиме мінімальне доповнення або ізоляція за погодженим scope. Критерій — completeness/readiness, а не обов'язкове видалення каталогу.
- `git diff --check` — PASS. Повний pytest/Browser тут не повторювали. Попередні 3873 PASS та інші історичні цифри нижче не є результатом цієї звірки.

### Уточнення попередніх відміток

- IMPLEMENTED нижче означає локальну реалізацію, а не виконання всіх умов GitHub completed.
- Для #470 окремі OS-процеси чи фізичний TTS не є новою умовою цього i: інтеграційний тест проходить фактичні CORE/Worker handlers з контрольованим backend.
- Для #474 миттєве переривання вже активного HTTP-запиту не було критерієм; межа зупинки задокументована.
- Для #482 конфігурація trusted proxy/global ceiling реалізує затверджені критерії; сама поява технічних knobs не доводить непогодженої продуктової функції.
- К1/В1–В6, описані нижче, не видаються за новий повний незалежний аудит: В1 і В3 охоплено цільовими тестами, інші позаскопові твердження не стали додатковими gates закриття цих 15 Issues.

Код, VERSION, CHANGELOG, tags та branches цією звіркою не змінювалися; усі попередні незакомічені зміни збережено. Змінено лише цей звіт, додано підсумки до GitHub Issues та наступників залишку.

---


## Висновок і межі перевірки

Версія 0.0.3.73 закрила основні архітектурні розбіжності попереднього аудиту: F1, F2 і F5 виправлено, F4 виправлено в межах Issue #467. Водночас виявлено:
- одне нове порушення P1: TTS-синтез виконується в CORE;
- одну проблему безпеки P2: заголовки mTLS можна підробити;
- кілька порушень P2 щодо архітектурних інваріантів, процесу релізів і генерації сценарію.

Production-готовність, як і раніше, не підтверджено: стендові перевірки не виконувалися.

Параметри перевірки:
- Версія: **0.0.3.73**.
- SHA: `1af8f9cd54090bef0408dff86627ff661bf59af9`. Локальний `HEAD` збігається з локальним ref `origin/main`. Remote через `git ls-remote` не звірено: команда повернула порожній результат.
- Робоче дерево: tracked-файли без змін. Є невраховані каталоги `.swarm/` і `.claude-flow/`, їх створили локальні інструменти агентів ruflo (див. R3).
- Метод: статичний аудит кодом і документацією чотирма незалежними напрямами (попередні знахідки, архітектурні інваріанти, безпека, релізний процес) і повний прогін `pytest`. Ключові знахідки P1/P2 перевірено вручну повторно.
- Не перевірено: GitHub Issues, статуси CI/Browser E2E, фактичні assets GitHub Release, remote tags. У середовищі немає `gh`, а remote tags недоступні.

Під час аудиту код, Issues і GitHub-статуси не змінювалися. Цей файл створено за прямим дорученням користувача.

## Статус знахідок аудиту 13.11.2026

| Знахідка | Issue | Статус | Доказ |
|---|---|---|---|
| F1. Structure → Scenes → Assembly у production | [#465](https://github.com/fylypovych/vertep/issues/465) | **Виправлено** | `execute_script()` викликає `providers.llm().generate_script()` (`worker/role_executor.py:112-137`). `ScriptAgent` виконує окремі етапи, точкову перегенерацію невалідної сцени (`SCRIPT_SCENE_MAX_ATTEMPTS`, типово 3) і нормалізацію через `ScriptDocument` (`core/script_agent.py:44-154`). Тести: `tests/test_script_stages.py` |
| F2. Сценарій через обраний LLMProvider | [#466](https://github.com/fylypovych/vertep/issues/466) | **Виправлено** | У production-шляху немає `OLLAMA_URL` і `/api/generate`. Backend обирається через `VERTEP_LLM_PROVIDER` (`adapters/llm_clients.py:137-162`), невідоме значення дає явну помилку без fallback. Тести: `tests/test_script_llm_provider.py` |
| F3. LAB executor лише звітний | [#469](https://github.com/fylypovych/vertep/issues/469) | Без змін — **BLOCKED** (15.11.2026) | Обмеження задокументоване. Змін у 0.0.3.73 немає. Реалізацію #469 (i.0.0.4.69) відкладено до встановлення LAB на реальну VM (рішення власника 06.11.2026, #398/#329); підтвердження передумови немає (#329 без коментарів), тому не реалізовувалось |
| F4. Кодовий контекст звітів LAB | [#467](https://github.com/fylypovych/vertep/issues/467) | **Виправлено в межах Issue** | `_collect_context()` повертає `coverage` і `limitations`, межі вставляються в prompt і у відповідь `/invoke` (`lab/role_service.py:29-170`). Без `paths` модель і далі бачить лише перелік файлів, але тепер це явно позначено. Тести: `tests/test_lab_report_context.py` |
| F5. Блокування event loop LAB API | [#468](https://github.com/fylypovych/vertep/issues/468) | **Виправлено** | Читання контексту й inference йдуть через `run_in_threadpool` (`lab/role_service.py:149,160`). Тести: `tests/test_lab_role_service_concurrency.py` |

## Нові знахідки

### A1. P1 — TTS-синтез виконується в процесі CORE

**Вимога:** §3.1 і §3.5 AGENTS.md. Будь-яке виконання генерації, зокрема TTS-синтез, іде лише через Worker або задокументований provider backend і ніколи не виконується напряму в CORE.

**Доказ:** `core/api/models.py:9` імпортує `worker.role_executor.synthesize_voice`. Ендпоінти `POST /api/models/voices/synthesize` (`:92-100`) і `POST /api/models/voices/preview` (`:110-183`) викликають його в CORE. Функція робить HTTP POST до TTS runtime (`worker/role_executor.py:223-233`) повз Worker dispatch і `TTSProvider`. У `docs/core-generation-gate.md` такого винятку немає.

**Чому gate не ловить:** `synthesize_voice` немає в `FORBIDDEN_TARGETS` (`tests/test_core_generation_gate.py`).

**Рішення власника:** перенести прев'ю голосу на Worker dispatch або задокументувати його як погоджений виняток.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#470](https://github.com/fylypovych/vertep/issues/470) i.0.0.4.70 — IMPLEMENTED. Preview і synthesis голосу ставлять у чергу job-less задачу `voice_preview` (`core/voice_dispatch.py`); Voice Worker виконує її через `execute_role_task()` і повертає audio з `audio_contract/v1`, CORE перевіряє sha256 і параметри. Без готового Voice Worker — 503, timeout — 504, помилка Worker — 502; fallback у CORE немає. `synthesize_voice` прибрано з CORE, gate забороняє його. Тести: `tests/test_voice_worker_dispatch.py` (CORE→Worker→TTS→audio через реальний HTTP stand-in), оновлено `tests/test_provider_switch.py`.

### A2. P2 — сліпі зони CORE Generation Gate

**Доказ:** `tests/test_core_generation_gate.py`:
- список заборонених цілей іменний: не покриває `synthesize_voice`, прямі `httpx`/`requests` до LLM/TTS/ComfyUI, `getattr` чи `importlib`;
- allowlist прив'язаний до файлу, а не до функції;
- сканується лише `core/`.

`core/gpu/backends.py:56,86,113` містить прямі HTTP-виклики inference. Фактично їх імпортує лише Worker (`worker/gpu_routing.py:23`), тож це структурний ризик, а не підтверджене виконання в CORE.

**Ризик:** нові порушення §3.1 проходитимуть gate непомітно, як A1.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#471](https://github.com/fylypovych/vertep/issues/471) i.0.0.4.71 — IMPLEMENTED. Gate розпізнає `synthesize_voice` і Worker-виконавці, клієнти з `with ... as`, `importlib.import_module`/`__import__`/`getattr` (неконстантний `getattr` на модулі генерації — окреме порушення), прямі HTTP inference-запити `httpx`/`requests` за endpoint. Allowlist прив'язано до enclosing function; `core/gpu/backends.py` дозволено як shared implementation з перевіркою, що його імпортує лише `worker/gpu_routing.py`. Межі статичного аналізу задокументовано в `docs/core-generation-gate.md` §3.1. Тести: `tests/test_core_generation_gate.py` (`TestBlindSpots`, `TestSharedImplementationBoundary`).

### A3. P2 — заголовки результату mTLS можна підробити через `location /`

**Доказ:** у `deploy/proxy.conf:11` блок `location /` не перезаписує `X-Vertep-Client-DN` / `X-Vertep-Client-Serial`. Їх виставляє лише regex-блок (`:12-19`) для heartbeat, claim/renew/cancellations/result, logs/ingest, node/status і renew. `core/security.py:64-91` при `NODE_MTLS_REQUIRED=true` довіряє цим заголовкам.

Маршрути з перевіркою вузла, які йдуть через `location /` (за звітом напряму безпеки):
- `/api/tasks/publisher-readiness`;
- `/api/tasks/{id}/inputs/...`;
- `/api/tasks/{id}/character-references/...`;
- `/api/workers/model-progress`;
- `/api/watchdog/report`;
- `/api/nodes/{id}/self-test`.

**Сценарій:** власник викраденого worker-токена без клієнтського сертифіката підставляє DN і serial вузла. mTLS як другий фактор на цих маршрутах перестає діяти.

**Обмеження:** токен вузла все одно потрібен. Повного обходу автентифікації немає.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#472](https://github.com/fylypovych/vertep/issues/472) i.0.0.4.72 — IMPLEMENTED. У `deploy/proxy.conf` і шаблоні `X-Vertep-Client-DN/Serial` в усіх locations беруться через `map` лише за `$ssl_client_verify = SUCCESS`, інакше не передаються. Тест `tests/test_proxy_client_identity.py` на реальному nginx (1.24 з пакета Ubuntu, не образ `nginx:1.27-alpine`) з реальним процесом CORE і `NODE_MTLS_REQUIRED=true`: підроблені заголовки без сертифіката й чужий сертифікат відхиляються, валідний cert+token проходить, Web/setup не регресують; на старому конфігу тест падає. Без nginx тест пропускається. Стендова перевірка на розгорнутому proxy лишається за #54.

### A4. P2 — спільний резервний `NODE_API_TOKEN`

**Доказ:** `core/security.py:78-83`. Якщо для вузла немає окремого токена, для будь-якого `node_name` приймається спільний `NODE_API_TOKEN`.

**Ризик:** витік одного токена дає змогу видати себе за будь-який вузол. Разом з A3 це послаблює захист machine-маршрутів. Чи задається змінна в production, не перевірено.

**Коментар виконання (15.11.2026):** [#473](https://github.com/fylypovych/vertep/issues/473) i.0.0.4.73 — у цій сесії не виконувався.

### A5. P2 — немає загального ліміту часу й перевірки скасування в генерації сценарію

**Доказ:** timeout застосовується до кожного HTTP-запиту (`OLLAMA_SCRIPT_TIMEOUT`, 300 с), а не до задачі (`core/script_agent.py:52-53,123-132`). Верхня межа — 1 + 200 сцен × 3 спроби запитів. У циклі сцен немає перевірки cancel/lease (`worker/role_executor.py:131`).

**Ризик:** скасована задача або задача з втраченою lease продовжує займати Text Worker і LLM. До 0.0.3.73 був один запит ≤300 с.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#474](https://github.com/fylypovych/vertep/issues/474) i.0.0.4.74 — IMPLEMENTED. `task.timeout` / `OLLAMA_SCRIPT_TIMEOUT` — загальний бюджет прогону (`ScriptBudget`); перед структурою, кожною сценою й повтором сцени перевіряються скасування, втрата lease і залишок бюджету; timeout поточного запиту обрізається до залишку, запит у польоті не переривається (межу задокументовано в `docs/content-pipeline.md`). Скасування дає `cancelled` result, вичерпаний бюджет — звичайну помилку. Тести: `tests/test_script_budget.py`, оновлено `tests/test_script_stages.py`.

### A6. P2 — у коді прописано персонажа за замовчуванням

**Вимога:** §3.2, §0.4: не хардкодити персонажів.

**Доказ:** `did_samogon` як значення за замовчуванням:
- `core/models.py:291`;
- `core/api/jobs.py:647`;
- `core/telegram/job_callbacks.py:78,254`.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#475](https://github.com/fylypovych/vertep/issues/475) i.0.0.4.75 — IMPLEMENTED. `did_samogon` прибрано з runtime-коду; default береться з нової змінної `DEFAULT_CHARACTER` (рішення власника), Telegram — з `TELEGRAM_DEFAULT_CHARACTER`, а якщо вона порожня — з `DEFAULT_CHARACTER`. Відсутній чи невалідний default дає 422 або повідомлення в Telegram, без мовчазного призначення. Тести: `tests/test_default_character.py`. Bootstrap не пише `DEFAULT_CHARACTER` у production `.env` — див. В5.

### A7. P2 — неповний каталог персонажа

**Доказ:** `characters/char-contract/` містить лише `character.json`, `system_prompt.txt`, `voice.json`. Відсутні `visual.json`, `generation.json`, `publishing.json` (§3.2). Каталог використовується тестом `tests/test_voice_issued_result.py`, але лежить серед production-персонажів. Runtime-перевірки повного набору файлів не знайдено.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#476](https://github.com/fylypovych/vertep/issues/476) i.0.0.4.76 — PARTIAL. Причина: `tests/test_voice_issued_result.py` записував неповного персонажа прямо в `characters/`; тепер — у `tmp_path`. Персонаж без усіх шести файлів §3.2 не з'являється в `GET /api/characters`, виборі Telegram і не може бути default; явне посилання за id працює як раніше. Тести: `tests/test_character_completeness.py`. Залишок: видалення закоміченого `characters/char-contract/` — лише з окремого дозволу власника.

### A8. P2 — блокуюче хешування артефактів на event loop

**Доказ:** `core/api/system.py:663-674`, `system_test`, викликає `read_bytes()` і sha256 усіх артефактів останніх job в `async def`. Обсяг не обмежено.

Менш вагомо (P3) — запис завантажених файлів в `async`:
- `core/api/jobs.py:608,669`;
- `core/api/settings.py:230`;
- `core/api/setup.py:146`.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#477](https://github.com/fylypovych/vertep/issues/477) i.0.0.4.77 — IMPLEMENTED. Хешування в `system_test` винесено в `asyncio.to_thread` з потоковим читанням по 1 MiB; блокуючий I/O в upload, import, logo і first-run (`create_node_csr` з openssl) — теж. Тести: `tests/test_system_test_off_loop.py`, зокрема concurrency-тест на спільному event loop (на синхронному варіанті падає).

### A9. P2 — відсутні release notes версії 0.0.3.65

**Доказ:** `releases/0.0.3.65.md` немає ні в робочому дереві, ні в історії. Commit `aeb4533a` (0.0.3.65) і блок у `CHANGELOG.md` існують. Порушено §2.5, §2.8.

**Обмеження виправлення:** заборона переписувати історію (§0.4) дозволяє лише додати файл новим версійним commit і задокументувати розбіжність.

**Коментар виконання (15.11.2026, локально, без commit/push):** [#478](https://github.com/fylypovych/vertep/issues/478) i.0.0.4.78 — IMPLEMENTED. Створено `releases/0.0.3.65.md` (`# Vertep 0.0.3.65`) за `git show aeb4533a` і `CHANGELOG.md`, з позначкою про пізнє відновлення; історію, SHA, tags і `VERSION` не змінено, файл увійде лише в наступний версійний commit за командою публікації. Тест: `tests/test_release_notes_0_0_3_65.py`.

### Знахідки P3

- Неузгоджена матриця провайдерів: невідомий `VERTEP_LLM_PROVIDER` у `provider_matrix` показується як `ollama` з `configured=True`, а виконання кидає помилку (`adapters/providers/__init__.py:420-430`). `openai` без ключа приймається як валідна доставка.
  - **Коментар виконання (15.11.2026, локально, без commit/push):** [#479](https://github.com/fylypovych/vertep/issues/479) i.0.0.4.79 — IMPLEMENTED. Спільне правило `backend_readiness()` для `provider_matrix` і активації на Worker: невідомий backend показується під власною назвою з `configured=false`, `openai` готовий лише з ключем; Worker без ключа більше не звітує openai застосованим (шлях відтворено). Тести: `tests/test_llm_readiness.py`.
- Неповна валідація структури сценарію: невалідні `title`, `description`, `hashtags`, `platforms` виявляються лише на Assembly після генерації всіх сцен (`core/script_agent.py:93-102,151`).
- `task["prompt"]` змінив семантику: тепер він лише brief, а не повний prompt (`core/script_agent.py:185-186`). Зараз CORE його не заповнює.
- `duration: null` у відповіді сцени спричиняє зайву перегенерацію сцени (`core/script_agent.py:142`).
  - **Коментар виконання (15.11.2026, локально, без commit/push) до трьох пунктів вище:** [#480](https://github.com/fylypovych/vertep/issues/480) i.0.0.4.80 — IMPLEMENTED. Метадані структури перевіряються контрактом Assembly (`normalize_script()`/`ScriptDocument`) одразу після Structure, до сцен; схему не послаблено. `duration: null` = відсутній ключ → запланована тривалість без повтору; невалідне число й надалі перегенеровує сцену. `task.prompt` — додатковий brief; producers (CORE, workflow runtime) його не задають, несумісності немає, семантику закріплено тестом і в `docs/content-pipeline.md`. Тести: `tests/test_script_structure_contract.py`.
- `code_examined = bool(coverage["read"])` завищує ознаку перевірки коду (`lab/role_service.py:76`).
- `read_text()` читає файли LAB повністю перед обрізанням, `rglob` обходить усе дерево (`lab/role_service.py:67`).
  - **Коментар виконання (15.11.2026, локально, без commit/push) до двох пунктів вище:** [#481](https://github.com/fylypovych/vertep/issues/481) i.0.0.4.81 — IMPLEMENTED. Файл читається не більше залишку ліміту контексту; обхід зупиняється на 500 файлах і не заходить у `.git`; `code_examined` істинне лише для непорожнього вмісту, порожні файли — `empty` і в `limitations`. Змінено контракт `files_read`: `total_chars` = `null` для обрізаного файла, додано `total_bytes`, `empty`; `listed_files` — кількість показаних. Тести: `tests/test_lab_report_context.py`; усі LAB-тести — 471 passed.
- Readiness publishers береться напряму через `PUBLISHERS`, повз `providers.publisher()` (`core/api/tasks.py:438`).
- `core/pull_executor.py:110` виконує Ollama pull з CORE. Це керування моделями, а не генерація.
- `docs/core-generation-gate.md` має застарілі номери рядків і згадку `core/app.py:848`.
- `get_best_provider(exclude=...)` (`core/gpu/router.py:123`) — примітив, придатний для failover. У production не викликається.
- Cookie сесії без `Secure` (`core/api/auth.py:281-283`). Пом'якшено тим, що UI доступний лише через TLS.
- Ключ сховища секретів створюється через `write_text`, а потім `chmod 0600` (`core/first_run.py:111-122`). Є коротке вікно з правами за umask.
- Ліміт спроб setup-токена рахується за IP proxy, тобто спільний для всіх клієнтів (`core/api/auth.py:51-72`). Можливе блокування майстра (DoS).
  - **Коментар виконання (15.11.2026, локально, без commit/push) до трьох пунктів вище:** [#482](https://github.com/fylypovych/vertep/issues/482) i.0.0.4.82 — IMPLEMENTED. Cookies сесії та CSRF отримують `Secure` за HTTPS (TLS або `X-Forwarded-Proto: https` від proxy), dev по http — без `Secure`. `secret-store.key` створюється з 0600 від початку (`O_CREAT|O_EXCL`), sealing і заборона перезапису збережені. Спільний lockout відтворено; ліміт тепер на реального клієнта за `X-Real-IP` лише від довіреного proxy (`TRUSTED_PROXY_HOSTS`, типово `proxy`) плюс загальна стеля `SETUP_RATE_LIMIT_GLOBAL_PER_10_MINUTES` (100); proxy перезаписує `X-Real-IP`. Нові змінні додано без окремого погодження власника. Тести: `tests/test_security_hardening_482.py`.
- `shell=True` з командами з env оператора (`services/backup_service.py:447,527,543,902`).
- `node-exporter` монтує `/` хоста з `pid: host` (`deploy/docker-compose.yml:327-332`).
- `release.yml` має input `skip_tests`. Dispatch з `scripts/release.py:420` іде через `--ref main`, а не конкретний SHA. SBOM входить у bundle, але окремим asset не публікується.

**Рішення власника щодо семи P3 без Issue (16.11.2026) і виконання (локально, без commit/push):**
- Readiness publishers повз `providers.publisher()` — **виправлено**. `GET /api/tasks/publisher-readiness` іде через `providers.publisher().available_channels()` і новий `PublisherProvider.readiness(channel, targets)`, без прямого `PUBLISHERS`. Тест: `tests/test_issues_427_429.py::test_core_readiness_goes_through_the_publisher_provider`.
- `core/pull_executor.py` (Ollama pull з CORE) — **залишено**: це керування моделями без генерації, правило CORE Generation Gate не порушено.
- `docs/core-generation-gate.md` — **виправлено**. Таблиця §5 посилається на функції, а не на номери рядків. Неіснуючий `core/app.py:848` прибрано: Telegram ставить публікацію в чергу. Додано `_enqueue_publish_task` і `publisher_readiness`.
- `get_best_provider(exclude=...)` — **залишено**: основа для можливого failover, видалення прибрало б можливість.
- `shell=True` у `services/backup_service.py` — **оформлено Issue** [#488](https://github.com/fylypovych/vertep/issues/488) (`i.0.0.4.88`). Під час оформлення додатково знайдено, що пароль Redis із `REDIS_URL` потрапляє в argv. **Виконано локально 16.11.2026**: вбудовані команди запускаються без shell, пароль Redis передається через `REDISCLI_AUTH`, `{file}`/`{path}` підставляються з `shlex.quote`; override оператора лишаються shell-рядками (`docs/backup-restore.md`). Тести: `tests/test_backup_commands_488.py`.
- `node-exporter` з `/:/host:ro,rslave` і `pid: host` — **прийнятий ризик** без змін: стандартна конфігурація за документацією node-exporter.
- `release.yml`/`scripts/release.py` — **виправлено**:
  - dispatch передає `expected_sha`; workflow зупиняється, якщо `main` уже не на перевіреному commit, а `scripts/release.py` відмовляється від run з іншим `headSha`;
  - `sbom-<версія>.cdx.json` публікується окремим asset, входить у `SHA256SUMS-<версія>` і в перелік обов'язкових assets `verify_release`;
  - `skip_tests` залишено лише для ручного запуску; якщо його використано, опис GitHub Release отримує позначку про пропущені тести.
  - Тести: `tests/test_release_hardening.py`, `tests/test_release.py`. Документація: `docs/versioning.md`.

## Знахідки виконавця поза Issues (15.11.2026)

Виявлено під час виконання #470–#479 (К1 — під час перевірки #469–#483); у scope жодного Issue не входять (§0.3, §4.4). Виправлено за прямим дорученням власника 15.11.2026 — див. «Виправлення знахідок поза Issues» нижче.

- **В1. P2 — machine-маршрути Worker блокує сесійний middleware.** `core/api/auth.py` не включає `/api/tasks/publisher-readiness`, `/api/workers/model-progress`, `/api/watchdog/report` і `/api/nodes/{id}/self-test` до списку machine-маршрутів. Коли auth увімкнено (`ADMIN_PASSWORD` або облікові записи), Worker отримує на них `401 Authentication required` ще до перевірки токена, хоча викликає їх (`worker/service.py:459`, `worker/model_commands.py:52`, `scripts/watchdog.py:184`). Відтворено під час `tests/test_proxy_client_identity.py`.
- **В2. P2 — відсутні release notes 0.0.3.7.** `releases/0.0.3.7.md` немає, блок у `CHANGELOG.md` є. Той самий тип порушення, що A9 (§2.5, §2.8).
- **В3. P3 — синхронна перевірка storage у `system_test`.** Пробний запис, `fsync` і видалення файла виконуються в `async def system_test` на event loop (`core/api/system.py`, блок `storage`); до scope #477 не входило.
- **В4. P3 — forwarder лабораторії не видимий для gate.** `core/api/lab_lease.py:inference` пересилає `POST /api/{operation}` на `LAB_GPU_ENDPOINT` з динамічним URL, тож статичний gate його не розпізнає. Це задокументовано як межу аналізу в `docs/core-generation-gate.md` §3.1; сам forwarder описано в `docs/lab/lab-gpu-contract.md`.
- **В5. Операційне — `DEFAULT_CHARACTER` відсутній у production `.env`.** Bootstrap не записує ні `DEFAULT_CHARACTER`, ні `TELEGRAM_DEFAULT_CHARACTER`. Після #475 варіант «За замовчуванням» у Web/API та Telegram без каталогу персонажів дає зрозумілу помилку, доки змінну не задано.
- **В6. Операційне — повний `pytest -n 8` не завершується в цьому середовищі.** Прогони обриваються з exit 137 приблизно на 14%, причину не встановлено (пам'яті достатньо). Зміни #470–#479 перевірено цільовими й суміжними наборами тестів, а не повним прогоном.- **К1. P1 — скасування fenced-оновлення вбиває всі процеси користувача.** `scripts/update-agent.py:_kill_process_group` викликав `/usr/bin/kill -SIG -PGID`; procps-ng 4.0.4 (Ubuntu 24.04) доставляє такий сигнал усім процесам користувача, не лише групі (відтворено безпечним `SIGCONT`: його отримав сторонній процес в іншій сесії). На хості з update-agent від root це зупинило б увесь хост; у цьому середовищі тест `test_run_fenced_stops_apply_and_delayed_side_effect_on_cancel_fence` вбивав процеси користувача — це й була причина В6. Знайдено під час перевірки #469–#483.

**Виправлення знахідок поза Issues (15.11.2026, локально, без commit/push):**
- К1 — виправлено: лишився лише `os.killpg` на виділену групу дочірнього процесу; групи `<= 1` і власна група відхиляються; виклики `kill`/`pkill` прибрано. Тести: `tests/test_update_agent_kill_scope.py` (ціль зупинено, сторонній процес живий); `tests/test_update_security.py` — 54 passed без впливу на інші процеси.
- В1 — виправлено: `GET /api/tasks/publisher-readiness`, `POST /api/workers/model-progress`, `POST /api/watchdog/report`, `POST /api/nodes/{id}/self-test` — точні machine-маршрути в `core/api/auth.py` (шлях + метод); перевірку токена виконує сам маршрут. Тести: `tests/test_worker_routes_auth.py`, розширено `tests/test_proxy_client_identity.py` (усі шість маршрутів на реальному nginx).
- В2 — виправлено: відновлено `releases/0.0.3.7.md` і знайдений тим самим способом пропуск `releases/0.0.2.91.md` з позначкою пізнього відновлення; історію не змінено. Тест повноти: `tests/test_release_notes_history.py`.
- В3 — виправлено: storage-probe `system_test` виконується через `asyncio.to_thread` (`_storage_probe`); тести в `tests/test_system_test_off_loop.py`.
- В4 — без змін коду: це межа статичного аналізу, задокументована в `docs/core-generation-gate.md` §3.1.
- В5 — виправлено без хардкоду персонажа: bootstrap записує `DEFAULT_CHARACTER`, якщо його передано під час встановлення, а значення з `.env` і так зберігається при оновленнях; описано в `docs/installation.md`.
- В6 — усунуто разом із К1: повний `python -m pytest -q -n 8 --dist loadgroup` — 3873 passed, 152 skipped, 0 failed.
- Не виконано: видалення `characters/char-contract/` (#476) — потрібна пряма команда власника на видалення.

## Підтверджено без зауважень

- **Worker і адаптери:** `worker/service.py` не створює `ComfyUIAdapter`/`TTSAdapter`, виконання йде через `execute_role_task()` і `providers.*`.
- **FFmpeg-монтаж** у CORE виконується через `ThreadPoolExecutor` (`core/state.py:22`).
- **GPU dispatch:** черговість через `core/turns.py`, пріоритетів вузлів немає, автоматичного failover GPU-провайдера немає (`core/gpu/router.py:37-88`).
- **Publishers:** окремі модулі для кожної платформи.
- **Секрети:**
  - у HEAD і вибірці історії секретів не знайдено;
  - `.env` ніколи не комітився;
  - tracked `.pem` — лише публічні ключі;
  - секрети інтеграцій шифруються scrypt + AES-256-GCM, видача обмежена каналом задачі.
- **Автентифікація й API:**
  - Registration Token одноразовий і має TTL;
  - API за замовчуванням закритий auth middleware;
  - CSRF захищено double-submit cookie з `SameSite=Strict`;
  - секрети порівнюються в константному часі.
- **Bootstrap:** перевіряє підпис manifest і SHA256 артефактів, образи прив'язуються за digest.
- **Production compose:** без `privileged` і `docker.sock`, з `read_only`, `cap_drop: ALL`, `no-new-privileges`. Назовні публікується лише proxy 8443.
- **Версіонування:**
  - `VERSION` = тема HEAD = `CHANGELOG.md` = `releases/0.0.3.73.md`;
  - 35 останніх commit пронумеровані послідовно, теми лише `<версія>`;
  - формат `CHANGELOG.md` коректний;
  - локальні tags вказують на commit відповідних версій.
- **Release workflow:**
  - gate вимагає зелених CI і Browser E2E для точного SHA;
  - workflow не створює commit і не переміщує tags;
  - публікація йде через draft з перевіркою assets;
  - `latest` не є джерелом production.
- **Runtime bundle** містить усі складові §2.14.

## Результати перевірок

- `python -m pytest -q -n 8 --dist loadgroup` в ізольованому venv (Python 3.12): **3681 passed, 4 failed, 147 skipped** за 165 с.
- 4 падіння — `tests/test_issues_404_414_416_followup.py::test_worker_loop_survives_a_disowned_task[*]`. Причина — ізоляція самого тесту, а не код продукту:
  - тест підміняє `service.time.sleep`, тобто глобальний `time.sleep`;
  - на хості з `nvidia-smi` (тут WSL) `subprocess.run(..., timeout=...)` у `gpu_info()` (`worker/service.py:494`) витрачає ці виклики `sleep`;
  - тест зупиняється раніше, ніж Worker робить claim.
  - Без `nvidia-smi` у `PATH` файл проходить повністю: **20 passed**.
  - Рекомендація: патчити `sleep` локально, а не глобальний `time`. Це P3.
- Браузерні E2E без запущеного сервера пропускаються, у 147 skipped.
- Статус CI і Browser E2E для `1af8f9cd` не перевірено. Commit має `[skip ci]`, `gh` недоступний.
- **Коментар виконання (15.11.2026):** [#483](https://github.com/fylypovych/vertep/issues/483) i.0.0.4.83 — IMPLEMENTED. Тест підміняє `time` лише в модулі `worker/service.py` (`_LoopTime`), глобальний `time.sleep` не змінюється; параметризовано з повільним fake `nvidia-smi` і без нього; assertions ownership/claim recovery не послаблено. Цільовий файл — 24 passed, суміжні Worker-тести — 90 passed без skips.

### Результати після виконання #470–#483 і виправлень поза Issues (15.11.2026, локальне робоче дерево на базі `1af8f9cd`, без commit)

- Повний `python -m pytest -q -n 8 --dist loadgroup` (Python 3.12, `.venv`): **3873 passed, 152 skipped, 0 failed** за 119 с. Skips — відсутні `redis-server`, PostgreSQL binaries, браузерні/Docker-стенди та nginx у `PATH`.
- Proxy-приймання `tests/test_proxy_client_identity.py` окремо на реальному nginx 1.24 (пакет Ubuntu, не образ `nginx:1.27-alpine`): **6 passed**.
- `python -m compileall -q core adapters worker scripts installer tests lab` і `git diff --check` — без помилок.
- До виправлення К1 повний прогін у цьому середовищі обривався з exit 137 приблизно на 14%: тест fenced update вбивав усі процеси користувача (див. К1).

## Перевірка виконання Issues #469–#483 (15.11.2026)

Звірка критеріїв приймання з кодом і тестами локального робочого дерева. Усе — без commit/push; Issues у GitHub не змінювалися й не закривалися.

| Issue | Ідентифікатор | Статус | Залишок / межі |
|---|---|---|---|
| [#469](https://github.com/fylypovych/vertep/issues/469) | i.0.0.4.69 | BLOCKED | Передумова (LAB на реальній VM) не підтверджена; не реалізовувалось |
| [#470](https://github.com/fylypovych/vertep/issues/470) | i.0.0.4.70 | IMPLEMENTED | Інтеграція CORE→Worker→TTS→audio перевірена в одному процесі з HTTP stand-in TTS, не окремими процесами |
| [#471](https://github.com/fylypovych/vertep/issues/471) | i.0.0.4.71 | IMPLEMENTED | Межі статичного аналізу задокументовані (В4) |
| [#472](https://github.com/fylypovych/vertep/issues/472) | i.0.0.4.72 | IMPLEMENTED | Реальний nginx 1.24, не production-образ; стенд — #54 |
| [#473](https://github.com/fylypovych/vertep/issues/473) | i.0.0.4.73 | НЕ ВИКОНАНО | Спільний `NODE_API_TOKEN` досі приймається при `NODE_MTLS_REQUIRED=true` (`core/security.py`) |
| [#474](https://github.com/fylypovych/vertep/issues/474) | i.0.0.4.74 | IMPLEMENTED | Запит у польоті не переривається (задокументовано) |
| [#475](https://github.com/fylypovych/vertep/issues/475) | i.0.0.4.75 | IMPLEMENTED | `DEFAULT_CHARACTER` треба задати в `.env`/bootstrap (В5) |
| [#476](https://github.com/fylypovych/vertep/issues/476) | i.0.0.4.76 | PARTIAL | `characters/char-contract/` не видалено (потрібна команда власника); за явним id неповний персонаж завантажується |
| [#477](https://github.com/fylypovych/vertep/issues/477) | i.0.0.4.77 | IMPLEMENTED | Storage-probe винесено окремо (В3) |
| [#478](https://github.com/fylypovych/vertep/issues/478) | i.0.0.4.78 | IMPLEMENTED | Файл увійде лише в наступний версійний commit |
| [#479](https://github.com/fylypovych/vertep/issues/479) | i.0.0.4.79 | IMPLEMENTED | — |
| [#480](https://github.com/fylypovych/vertep/issues/480) | i.0.0.4.80 | IMPLEMENTED | — |
| [#481](https://github.com/fylypovych/vertep/issues/481) | i.0.0.4.81 | IMPLEMENTED | Змінено формат `files_read` |
| [#482](https://github.com/fylypovych/vertep/issues/482) | i.0.0.4.82 | IMPLEMENTED | Нові змінні `TRUSTED_PROXY_HOSTS`, `SETUP_RATE_LIMIT_GLOBAL_PER_10_MINUTES` без окремого погодження |
| [#483](https://github.com/fylypovych/vertep/issues/483) | i.0.0.4.83 | IMPLEMENTED | — |

Цільові тести всіх Issues разом: 287 passed без skips (proxy — на реальному nginx); повний прогін — у розділі «Результати перевірок».

## Що потребує реального стенду

Без змін відносно аудиту 13.11.2026:
- GPU passthrough і VRAM;
- production-priority GPU lease для LAB;
- ізоляція LAB VM;
- install, update, rollback, restore;
- наскрізний медіаконвеєр;
- Publisher receipts;
- LAB 24/7.

Окремо для A3: потрібна негативна перевірка на розгорнутому proxy, тобто запит із підробленими заголовками без клієнтського сертифіката.

## Пріоритети

1. A1: рішення власника щодо TTS-прев'ю в CORE (перенести на Worker або задокументувати виняток). Разом із цим — закрити сліпі зони gate (A2).
2. A3 і A4: очищати `X-Vertep-Client-DN/Serial` у `location /` (`deploy/proxy.conf` і шаблон). Не приймати спільний `NODE_API_TOKEN` при `NODE_MTLS_REQUIRED=true`.
3. A5: загальний бюджет часу й перевірка скасування в генерації сценарію.
4. A6–A8: прибрати персонажа за замовчуванням з коду, винести або доповнити `char-contract`, перенести хешування артефактів з event loop.
5. A9: додати `releases/0.0.3.65.md` у наступному версійному commit.
6. Виправити ізоляцію тесту `test_worker_loop_survives_a_disowned_task`.

Пріоритети є висновками аудиту, а не дозволом змінювати код, створювати Issues, виконувати push або release.

**Стан на 15.11.2026 (локально):** пункти 1–6 виконано через #470–#472, #474–#478, #483, крім A4 (#473, не виконано) і видалення `char-contract` (#476). Додатково виконано P3 #479–#482 і виправлено К1, В1–В3, В5. Відкрите: #473; видалення `characters/char-contract/` за рішенням власника; #469 після підтвердження передумови; публікація змін — лише за командою `пуш`/`тест`/`реліз`.

## Примітки

- **R1.** Аудит виконано паралельно чотирма агентами ruflo. Знахідки A1, A3, A9 і причину падіння тестів перевірено повторно вручну. Решту наведено за звітами агентів з посиланнями на код.
- **R2.** Статуси Issues #465–#468 у GitHub не змінювалися і не звірялися.
- **R3.** У робочому дереві з'явилися невраховані `.swarm/` (AgentDB/ruvector) і `.claude-flow/`, їх створили hooks ruflo. Перед `реліз` `git status` має бути чистим (§2.10): ці каталоги потрібно додати в `.gitignore` або видалити за рішенням власника.
