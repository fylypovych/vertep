# Перевірка #365 та #366

База `main`: `a98e2de4a19a6906c9403e8495a37275baa8f445`, VERSION `0.0.2.87`.
Перевіряється незакомічене робоче дерево; попередні зміни #360–364 збережено.
Точний SHA бази не означає опублікований SHA цих локальних змін.
Додатковий source manifest: `.pytest_tmp/issue-365-366-source.json`,
tracked diff SHA256 `9b24123e0328090739ee5e57ef70f204cb35cba7d0657042bef14c0fd2988775`;
manifest також містить SHA256 нових Python modules і route snapshot.

## #365 — pinned upstream

Статус: **BLOCKED** для обов'язкового pinned execution.

На цьому Windows host немає `docker`, WSL не встановлено; endpoint/image inventory
MoneyPrinter не налаштовано. Погоджений pin:
`harry0703/MoneyPrinterTurbo@2e1b30396e059e55939cc802c60faac2061e4d41`.
Digest справді зібраного образу відсутній, тому вигадувати його або зараховувати
stand-in як upstream заборонено.

За уточненням користувача підготовлено додатковий контейнер:
`docker-compose.moneyprinter.yml`, optional profile `moneyprinter`, той самий
pinned Dockerfile, Linux/amd64, окремий persistent tasks volume, Docker secret,
digest gate, без platform env_file і published ports. Додано validation до CI,
інструкцію build/start у `docs/moneyprinter-runtime.md`; `.dockerignore` виключає
локальні execution artifacts.

Офіційний standalone Docker Compose **5.6.1** завантажено з `docker/compose`,
checksum звірено з release `checksums.txt`. Його SHA256 є в source manifest.
Standalone profile config — PASS; merge з локальним stack — PASS
з `--no-env-resolution`, бо checkout не має `.env`. Жодні реальні env secrets
не підставлялися. Profile/copy/release-gate tests: **10 passed**, 11.71 s.
Фактичний `compose build moneyprinter` спробувано: **BLOCKED**, відсутня
`npipe:////./pipe/docker_engine`; також не встановлено BuildKit/buildx.
Артефакт: `.pytest_tmp/issue-365-build-attempt.log`. Image digest не вигадано;
build, container start та pinned execution не оголошуються PASS.

| ID | Existing implementation / перевірка | Expected / actual |
| --- | --- | --- |
| AC1 | `services/moneyprinter_service.py`, `adapters/providers/video_engines.py`, `tests/test_mpt_media_matrix.py` | Потрібний справжній pinned submit/status/download із decoder matrix; BLOCKED runtime |
| AC2 | `tests/test_assembly_worker_route.py`, CORE tasks API, Worker assembly executor | Потрібен pinned upstream на окремому process route; stand-in unit/regression не зараховано як повний PASS |
| AC3 | `tests/test_mpt_recovery.py`, wrapper durable mapping | Потрібні незалежні CORE/Worker/bridge restarts та фактичний upstream submission count; BLOCKED runtime |
| AC4 | `tests/test_moneyprinter_runtime.py`, existing early-audio staging | Unit regressions зберігаються; справжній pinned task до/після staging restart не виконаний |
| AC5 | `scripts/issue-122-acceptance-report.py` | Чинний report залишається єдиним механізмом; missing runtime не є PASS |

У чинний report додано `evidence_kind`: `unit_or_disposable` та `pinned_runtime`.
JSON і Markdown явно розмежовують ці докази, незалежно від доступності runtime.
Regression захищає від позначення stand-in recovery як pinned навіть при доступному runtime.
`real_submit_route --require-all` завершився ненульовим кодом:
**PASS 0 / FAIL 0 / NOT_RUN 1**, причина `no container runtime on this host`.
Артефакти: `.pytest_tmp/issue-365-runtime-final/issue-122-p10-acceptance.{json,md}`.
Unit/disposable report: **PASS 3 / FAIL 0 / NOT_RUN 1**; три PASS не зараховуються як AC1–4 pinned execution.
Report regressions: **24 passed**, `.pytest_tmp/issue-365-report-verified.log`.
Початкові wrapper/media/recovery regressions: **197 passed, 1 skipped**, 875.79 s,
`.pytest_tmp/issue-365-regression.xml`. Skip — decoder geometry test: спочатку
`ffmpeg` відсутній у PATH; повтор із наявним imageio FFmpeg binary дійшов до
відсутнього `moviepy`. Обидва skip не зараховано як PASS.
Після встановлення MoviePy 2.2.1 в ізольований `.pytest_tmp/issue-365-moviepy`
і використання наявного imageio FFmpeg 7.1, той самий decoder test:
**1 passed**, 3.21 s, `.pytest_tmp/issue-365-decoder-verified.log`.
Це Windows/Python 3.10 unit decoder proof, не execution Linux pinned image/lock.

Під час full suite виявлено рекурсивне копіювання власного basetemp:
`_repo_copy` включав `.pytest_tmp` у копію репозиторію. Execution artifacts тепер
виключено; новий regression копіює fake source у вкладений disposable каталог
і доводить відсутність self-copy. Продуктові файли/negative release gates не виключаються.
Copy/release-gate група: **8 passed**, 8.49 s. Повторний повний MPT regression:
**198 passed, 1 skipped**, 54.77 s, `.pytest_tmp/issue-365-isolated.xml`.

Автоматична перевірка відхилила cleanup власних рекурсивних копій (`blocked by policy`),
зокрема повтор із literal path. Видалення не виконано, originals збережено.
Залишилися створені цим завданням тимчасові копії:
- `.pytest_tmp/issue-366-full/test_release_gate_passes_on_th0/repo`
- `.pytest_tmp/issue-366-final/test_release_gate_passes_on_th0/repo`
- `.pytest_tmp/issue-366-verified/test_release_gate_passes_on_th0/repo`

Команди:

```powershell
python -m pytest -q tests/test_moneyprinter_runtime.py tests/test_mpt_recovery.py tests/test_mpt_media_matrix.py --tb=short --junitxml=.pytest_tmp/issue-365-regression.xml
python scripts/issue-122-acceptance-report.py --only media,integration,recovery,real_submit_route --out .pytest_tmp/issue-365-pinned-report --require-all
```

Physical VRAM та чинні real-server Issues не дублюються. Не створювалися інші
engines, fake acceptance або заміна погодженого pinned контракту.

## #366 — composition root

Маршрути snapshot знято з фактично імпортованого застосунку до extraction і
збережено в `tests/fixtures/api-routes-before-366.json`. Guard порівнює **весь**
набір method/path, включно з mounts, з фактичним застосунком після extraction.

| ID | Реалізація / test | Expected / actual |
| --- | --- | --- |
| AC1 | `core/api/auth.py`, `events.py`, `telegram.py`, `status.py`, `core/lifecycle.py`, `core/web_ui.py`; `test_all_method_path_contracts_match_the_before_extraction_snapshot` | Ті самі маршрути/session/state; snapshot збігається |
| AC2 | `core/telegram/handlers.py`, `core/api/_internal.py`; migrated private callers | Немає `core.app` imports, `sys.modules`, runtime attribute lookup або root re-export shims; dispatch table/wait timeouts збережено |
| AC3 | `core/app.py`, `tests/test_app_structure.py` | Composition only, ≤300 lines, відсутні endpoint/business definitions, одна дозволена top-level assignment `app = FastAPI(...)` |
| AC4 | Generation gate, Telegram durable tests, full suite, compileall/diff-check | Не додаються routes/roles/UI; повний результат записується після завершення перевірок |

Доменні timeout/recovery функції перенесено до `core/runtime.py`, без зміни
generation gate. Telegram lifecycle і mutable polling state мають власника handlers;
lifespan імпортує його функції прямо. Системні API залишаються у своїх чинних routers.
Спільні sync/async internal HTTP helpers залишаються в `core/api/_internal.py`.
`core/app.py` має 54 непорожні рядки; ліміт guard застосовується до всіх рядків файла.

Приватні test callers і monkeypatch targets перенесено до фактичного owner/consumer
у тій самій фазі. Старі inventory-only Full Self-Test очікування оновлено до
вже реалізованого #364 fail-closed contract; вони не симулюють execution evidence.
Видалено мертві скопійовані tails, що посилалися на неіснуючі locals, і визначено
callback ID у двох наявних node-test handlers. Нових продуктових сценаріїв немає.

Відомий до refactor baseline `test_backup_browser_telegram_rbac` шукав `auth`
у `services/backup_service.py`, хоча guard належить API/Telegram. Перевірку
замінено фактичними TestClient і Telegram callback викликами: viewer 403,
READ_ONLY 423 без зовнішнього backup call, admin 200, Telegram non-admin denied,
admin success та authenticated internal call 423 у READ_ONLY. Контролюється лише
зовнішній Backup Service/Telegram transport; тест не підміняє CORE guards.
Цільовий прогін: **1 passed**, `.pytest_tmp/issue-366-backup-rbac.log`.

Проміжні повні прогони з попередніми test callers та рекурсивним copy fixture
зупинено після локалізації причин; вони не зараховуються як завершений full suite.
Остаточний прогін:
`python -m pytest -q --tb=short --basetemp=.pytest_tmp/issue-366-fenced -o junit_family=legacy --junitxml=.pytest_tmp/issue-366-fenced.xml`.
У ньому `VERTEP_REQUIRE_NODE_PROCESS=1` та `VERTEP_REQUIRE_MEDIA_BROWSER_E2E=1`;
PostgreSQL binaries, Redis, Node та OpenSSL доступні через відповідні test env/PATH.

Проміжний full suite `.pytest_tmp/issue-366-isolated.xml`: **3098 passed,
1 failed, 125 skipped**; єдиний failure — старий TTS fixture, що повертав RIFF
і для `--voices`. Тепер controlled CLI повертає коректний voice catalog та
декодований PCM WAV; позитивний route test — **1 passed**.

У спільному tail прогоні було **1436 passed, 2 failed, 19 skipped**, включно
з intermittent duplicate POST controlled GitHub test. Окремий весь
`test_real_tests.py` — **139 passed**. Виявлено недренований `_run_executor`
Real Tests і недостатній старий no-op fence: один вільний thread міг виконати
обидва no-op, поки інший продовжував роботу. Test teardown тепер fencing-ує
обидва threads обох executor до `monkeypatch.undo`, а нормальний storage reset
виконує після undo. Regression тримає один worker зайнятим і перевіряє, що drain
не завершиться завчасно. Спільна перевірка executor/Real Tests/role services/
structure/session contracts — **176 passed**, 18.63 s.

Середовище: Windows, Python 3.10.11, Node 24.15.0, Chromium Playwright 1.62.0,
локальні Redis 8.10.3 / PostgreSQL 17.6 / OpenSSL 3.5.8 для disposable перевірок.

Проміжні перевірки:
- API/session/structure/generation/Telegram focused: **163 passed, 3 skipped**.
- Generation/session/Telegram system/approval/durable: **115 passed, 1 skipped**.
- Окремий durable replay/actor/target/restart прогін: **13 passed**, 193.04 s.
- `compileall`, `git diff --check`: PASS.

## Остаточний результат

- Повний pytest: **3101 passed, 125 skipped, 0 failures / 0 errors**, 2491.37 s;
  `.pytest_tmp/issue-366-fenced.xml` і `.log` (3226 collected cases).
- Structural guards: **7/7**, generation gate **9/9**, durable Telegram
  replay/actor/target/restart **13/13**, provider process/Browser **6/6**,
  node process/Browser **10/10**, executor isolation **1/1**, report **24/24**;
  у цих групах немає skip.
- `compileall` і `git diff --check`: **PASS**.
- 125 skip не зараховано як PASS: 86 legacy Browser cases потребують окремого
  server на фіксованому port 8080; 16 потребують host espeak; решта — Docker/GPU/
  scrape, окремий PG DSN, host FFmpeg або POSIX/Linux-only checks. Повний перелік:
  `.pytest_tmp/issue-366-skip-reasons.json`. Disposable Node suite перевіряв власний
  реальний PostgreSQL; відсутній загальний DSN не видається за виконання старих DB tests.
- Під час остаточного прогону змінено лише інструкцію standalone container start
  та відповідний YAML comment; API/runtime/test код залишався незмінним.

**#366: IMPLEMENTED локально.** AC1–4 підтверджені на вказаній базі та dirty source
manifest: method/path snapshot збігається, root не має endpoint/business definitions,
runtime coupling/shims прибрано, відповідні regressions і повний suite green.

**#365: PARTIAL.** Додатковий контейнер/profile, build/config інструкції й reporting
підготовлено; schema/merge/unit regressions PASS. Погоджена actual pinned matrix та
independent CORE/Worker/bridge recovery proof **BLOCKED**: Docker Engine/BuildKit
на цьому host відсутні. Stand-in тести і Compose validation не зараховуються як
pinned execution. Runtime digest/submission count не вигадувалися.

Commit, push, branch creation, workflow запуск і зміни GitHub Issues не виконувалися.
