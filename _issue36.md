## Мета та підстава
Усунути підтверджені аудитом прогалини реалізації та автоматизованого acceptance у #6 (`i.0.0.0.3`), #16 (`i.0.0.0.13`) і #32 (`i.0.0.0.30`). Це зведене implementation Issue для конкретних дефектів із повторної перевірки; пов'язані загальні роботи не дублювати.

Базова перевірена версія: `0.0.1.45`, commit `5f5fbca7059cc4f8d42ca0eb0a9f2f7e8535d992`. Перед реалізацією повторно звірити поточний код: частину виправлень могли вже виконати в пов'язаних Issues.

## 1. Image storyboard та approval — #6
- Закрити обхід approval через Resume/Retry та інші повторні запуски: `NEW` із готовим script не повинен запускати video assets без затвердженого актуального image storyboard. Відтворено виклик `_dispatch_assets` для `task_type=video` з `image_status=ready` після Resume (`core/api/job_helpers.py`, `core/api/jobs.py`).
- Усунути колізію шляхів preview: різні storyboard versions із `image_version=1` пишуть той самий `storyboard/v1/scene-001-v1.*`. Відтворено зміну checksum старого artifact. Шлях та історія повинні враховувати storyboard version і image version (`core/image_storyboard.py`).
- Перевіряти очікувану image version при approval/revision/regenerate. Поле `ImageStoryboardAction.image_version` зараз ігнорується; запит для v1 може затвердити v2. Web UI та Telegram callbacks повинні передавати версію, яку користувач фактично переглянув (`core/api/storyboards.py`, `core/storyboard_telegram.py`).
- Завершити Telegram controls окремої сцени: `image_storyboard_keyboard` визначена, але не використовується. «Правки превʼю» повинні викликати image revision, а не перегенерацію всієї текстової розкадровки. Після часткової revision показувати також незмінені сцени за фактичними artifact references; не шукати всі файли лише в каталозі нової image version. Узгодити підтримувані формати зображень (`core/app.py`, `core/storyboard_telegram.py`).
- Показувати самі scene images у Web UI storyboard, а не лише artifact ID і download link (`web-v2/src/app/jobs/job-detail.component.ts`).

## 2. Persistence та міграція existing installation — #32
- До заміни старого контейнера зберігати його користувацькі `/app/characters`, `/app/brands`, `/app/workflows` та переносити їх у persistent roots без перезапису/видалення. Поточна startup-ініціалізація читає `/app/...` уже нового контейнера; update backup охоплює `/data/storage` і `/data/config`, але не старі ephemeral roots (`core/persistent_data.py`, `scripts/vertep`).
- Зберегти first-init seed semantics: наступні update/recreate не відновлюють видалені користувачем записи.
- Узгодити workflow references CORE/Worker з persistent roots та deployment-конфігураціями. Відносний `workflows/image/demo.json` зараз перевіряється адаптером відносно іншого root; відтворено `ValueError: Workflow path escapes WORKFLOWS_ROOT` (`worker/role_executor.py`, `adapters/comfyui.py`, `adapters/providers/compute_backends.py`).
- Перевірити фактичну міграцію jobs/channels між file backend і PostgreSQL, повторний запуск та відсутність втрати актуальних записів. Наявність backfill-файла або mock runner не вважати доказом виконання.

## 3. Backup/Restore — спільні дефекти #16 та #32
- Забезпечити повний backup DB, jobs/artifacts, персонажів, брендів, workflow, configuration, encrypted secrets і потрібного runtime state. Backup image не встановлює `pg_dump`, `pg_restore`, `redis-cli`; помилки dump ігноруються. Обов'язковий dump не може бути пропущений із успішним результатом (`docker/backup/Dockerfile`, `services/backup_service.py`, `deploy/docker-compose.yml`).
- Inventory має підтверджувати фактичний склад архіву та обов'язкові ресурси, включно з БД, а не лише перелік каталогів `_sources()`.
- Реалізувати дієвий failure path до безпечного системного стану: `_set_emergency()` звертається до `POST /api/system/state`, але CORE має лише GET. Перевіряти реальну зміну стану, а не виклик mock-функції.
- Узгодити server-side gating із Web UI; виключити restore поверх активних записів у NORMAL/UPDATING та забезпечити контроль стану протягом операції. Недоступність state/health API не повинна трактуватися як підтвердження безпечного restore.
- Вимагати успішної post-restore health verification. Відтворено `200 done`, коли post-restore health повертає `None`.
- Відновлювати точний склад snapshot, включно з порожніми каталогами: відтворено збереження нового файла в storage після restore snapshot, де storage був порожнім.
- Перевіряти результат remote copy та відображати помилки політики remote storage; зараз код завершення/винятки ігноруються, а receipt позначає remote copy лише за наявністю команди.
- Забезпечити штатний Web UI create/restore/progress/error flow. Зараз polling починається лише після завершення синхронного restore, а CORE proxy має timeout 120 секунд. Progress має відображати поточну операцію, зокрема тривалу, та її остаточний результат.

## Acceptance
- [ ] Автоматизований integration-сценарій проходить script approval → scene images → revision окремої/всіх сцен → regenerated images → image approval → video; негативні сценарії Resume/Retry та stale image approval не запускають незатверджене відео.
- [ ] Дві storyboard versions і кілька image revisions зберігають різні реальні файли, незмінні старі checksums та зв'язок scene → prompt → artifact → version; історія доступна після повторного завантаження даних.
- [ ] Web UI та Telegram tests перевіряють відображення зображень, controls окремої/всіх сцен, правильну обробку revision і stale callbacks.
- [ ] Existing-install migration harness переносить користувацькі файли зі старого ephemeral root до його заміни; конфлікти не перезаписуються, повторна міграція безпечна, видалені записи не повертаються через seed.
- [ ] CRUD персонажа, бренду, workflow і Job з подальшим recreate/update simulation перевіряється через новий процес/repository instance, включно зі змінами та видаленнями всіх типів ресурсів.
- [ ] Worker/ComfyUI contract завантажує workflow з persistent root за reference, який реально передає CORE; DEMO_MODE не обходить перевірку цього сценарію.
- [ ] Реальний backfill jobs/channels перевіряється на тимчасовій PostgreSQL через читання відновлених записів, включно з повторним запуском і конфліктами.
- [ ] Backup → зміна/видалення/додавання даних → restore перевіряє повний inventory, вміст файлів і записи DB; порожні каталоги також відновлюються коректно. Успішний mock `pg_restore` не замінює перевірки даних.
- [ ] Перевірено checksum/integrity, недоступний dump, remote-copy failure, частковий restore, недоступний post-health та фактичний системний стан після помилки; неправдивий NORMAL/успіх виключено.
- [ ] Web UI integration/E2E перевіряє create/restore/live progress/error, включно з операцією довшою за timeout звичайного proxy-запиту.
- [ ] Automated update/rollback compatibility harness працює з доступними release/runtime artifacts і користувацькими даними; перемикання штучних каталогів з `application.txt` не є достатнім доказом.

## Наявні перевірки та межі доказів
- Storyboard: `7 passed`; тести не виявляють наведені обходи й перезапис реальних файлів.
- Persistence/backfill/release-layout: `12 passed, 3 skipped` на Windows; recreate та migration coverage неповні, release-layout перевіряє синтетичні каталоги.
- Вибрані backup-тести: `8 passed`; два були локальними незакоміченими доповненнями в `tests/test_role_services.py`. Не зараховувати їх як опубліковані докази без перевірки main. Mock `_set_emergency` не підтверджує зміну стану CORE.

## Пов'язані роботи та межі
Пов'язано: #6, #16, #32; узгодити з #7, #8, #12, #15, #23, #24 та #33. Спільні backup-дефекти виправляти один раз із перевіркою критеріїв обох вихідних Issues.

Це implementation Issue. Фізичний GPU, окремий фізичний Backup Node та ручне руйнування production installation не потрібні для закриття. Фактична infrastructure qualification залишається в #35 (`rt.0.0.0.32`). Закриття — після реалізації, автоматизованих перевірок і потрапляння змін у main. Це Issue не надає автоматичного дозволу на push/release.