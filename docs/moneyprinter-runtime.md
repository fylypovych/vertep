# Ізольований runtime MoneyPrinterTurbo

> Статус: чинний | Аудиторія: розробник, оператор | Канон: `AGENTS.md` §28, Issue #122 P2

`money-printer` — опційний зовнішній `VideoEngine`. Vertep не генераєє ним контент
напряму: Job-цикл, персонаж, матеріали й публікація лишаються в Vertep, а
MoneyPrinterTurbo — це лише рендер зовнішнім бекендом.

## Чому окремий контейнер

Pinned upstream (`harry0703/MoneyPrinterTurbo@2e1b3039…`, MIT) не є частиною
Vertep- образів і не потрапляє в жодну роль в `config/node_roles.json`. Нативна
інсталяція його не тягне, не будує і не запускає.

- образ `ghcr.io/fylypovych/vertep-moneyprinter:<VERSION>` (лише `linux/amd64`);
- compose-профіль `moneyprinter` — звичайний `docker compose up` його не піднімає;
- ключ генерує `bootstrap.sh` у `config/moneyprinter-api.key` (права `0600`), але
  сам профіль лишається вимкненим.

Увімкнення:

```bash
COMPOSE_PROFILES=moneyprinter docker compose up -d moneyprinter
```

Жоден host-порт не публікується: рантайм доступний лише всередині compose-мережі.

## Розкладка

| Шлях | Вміст |
|---|---|
| `/opt/moneyprinter` | pinned upstream + `config.lock.toml` + `runtime-inventory.json` |
| `/opt/vertep` | Vertep wrapper (`services/`, `adapters/`) |
| `127.0.0.1:8080` | upstream API, тільки loopback усередині контейнера |
| `0.0.0.0:8098` | Vertep wrapper — єдиний зовнішній endpoint |

Upstream не бачить `VERTEP_*`, `JWT_SECRET` чи будь-які інші секрети платформи:
контейнер отримує лише власний `moneyprinter_api_key` через Docker secret.

## Fail-closed гарантії

Пін (repository, commit) живе в одному місці —
`adapters/providers/runtime_manifest.py` (`PINNED_UPSTREAM_REFERENCE`), який
`_contract()` wrapper-а читає напряму. `MONEY_PRINTER_CONTRACT` імпортує той самий pin, тому розбіжність неможлива
структурно: образ переносить лише `adapters` і `services`, а імпорт
`video_engines` у контейнері просто недоступний.

Контейнер не піднімається або не вважається готовим, якщо:

- не задано `VERTEP_MONEYPRINTER_IMAGE_DIGEST` або він не `sha256:<64>`;
- `runtime-inventory.json` відсутній, не парситься або має не той формат;
- upstream commit, `bridge_version` чи `bridge_schema_version` розійшлися з
  `MONEY_PRINTER_CONTRACT`;
- хеш будь-якого pinned-файлу не збігається з `file_digests`;
- upstream не відповідає `401` без `x-api-key`;
- у `TaskVideoRequest` немає обов'язкових полів submit-контракту;
- FFmpeg недоступний або короткий compose-рендер не дає декодованого виходу.

Причини повертаються стабільними кодами (`upstream_unreachable`,
`upstream_unauthenticated`, `upstream_schema_unsupported`, `ffmpeg_unavailable`,
`media_pipeline_unavailable`, `runtime_inventory_unverified`,
`engine_snapshot_mismatch`) і HTTP 503. Тихий відкат на `native` заборонений.

## Endpoint-и wrapper

| Endpoint | Призначення |
|---|---|
| `GET /health` | дешева готовність: snapshot + доступність upstream |
| `GET /runtime` | pinned snapshot: commit, версії, digest, інвентар залежностей, capability voice staging |
| `GET /self-test` | повний gate Issue #122 §9.12 |
| `GET /sbom` | SBOM поточного образу, згенерований із перевіреного інвентаря |
| `POST /api/v1/videos` | proxy submit |
| `POST /api/v1/video_materials` | proxy завантаження сцени: wrapper сам збирає multipart із сирих байтів і `x-vertep-filename` |
| `GET /api/v1/tasks/{id}` | proxy статусу |
| `DELETE /api/v1/tasks/{id}` | proxy скасування |
| `GET /api/v1/download/{path}` | proxy завантаження результату |

`MoneyPrinterEngine.health_check()` перевіряє доступність endpoint-а й, якщо
рантайм публікує інвентаризацію, звіряє її з pinned-контрактом. Розбіжність
знімає `available`, а не перемикає движок.

## Доставка артефактів (P3)

- Wrapper — єдиний зовнішній endpoint, тому сцени їдуть сирими байтами з
  іменем у заголовку `x-vertep-filename`, а multipart формує wrapper. Ім'я
  проходить суворий allowlist (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, без `..`),
  бо воно пишеться в multipart-заголовок.
- Відповідь upstream розгортається з реальної envelope-форми
  `{status, message, data:{file}}`; саме `data.file` (immutable storage key), а не
  top-level `filename`, іде в submit.
- Кожна спроба володіє власним staging-каталогом
  `.<output-stem>-<attempt token>-bridge`: два attempt-и одного Job не ділять
  staged-кліпи чи storage keys. Каталог видаляється в `finally`, зокрема коли
  submit, poll чи download упали.
- Submit не містить жодного шляху CORE: матеріали йдуть storage keys, а
  `custom_audio_file` порожній.
- Імпорт результату: тимчасовий файл → перевірка розміру → декодування
  (ffprobe, з допуском drifts) → `apply_post_step` (мікс voice/BGM, SRT,
  watermark, preset) → повторна перевірка → атомарний `replace` →
  перевірка читання + SHA-256 sidecar. `.tmp` видаляється і на успіху, і на
  помилці; недекодований або нечитабелий файл не стає версією.

## Статус P3

Виконано: multipart-доставка сцен через wrapper, реальний envelope `data.file`,
task-scoped власність staging-каталогу attempt-а, verified import із
checksum/size/media/access, негативні кейси (порожній, недекодований, нечитабельний
download, обмежені retry), different-root приймання.

Approved voice (§9.3 рядок 5) доставляється wrapper-ом через
`POST /api/v1/voice`: байти зберігаються content-addressed (SHA-256 у назві) і
`_stage_task_voice()` кладе їх у task-каталог pinned-резолвера перед submit, після
чого `resolve_custom_audio_file` приймає шлях у task-каталозі. Підміна TTS
заборонена §9.6, тому `task_local_voice_capability()` перевіряє саме цей маршрут
і fail-closed відхиляє render з `upstream_voice_staging_unsupported`, якщо staging
не довів файл до pinned-резолвера. `POST /api/v1/audio` не використовується.

## Конфігурація

Upstream читає `<app-root>/config.toml` — саме туди entrypoint рендерить
`docker/moneyprinter/config.lock.toml`, підставляючи лише `app.api_key`, і потім
звіряє решту з lock-файлом. Рендер у будь-який інший шлях залишив би API на
upstream-дефолтах, тобто тихий drift.

Зафіксовано: `upload_post_*` вимкнено (публікацією займається Vertep),
`enable_redis=false`, `video_source="local"`, `material_directory="task"`,
`subtitle_provider=""`, `endpoint=""`, upstream listener — `127.0.0.1:8080`.
Будь-який drift з lock-файлом зупиняє контейнер.

Тому `/opt/moneyprinter` лишається writable: upstream зберігає конфіг атомарно
через temp-файли в тому самому каталозі. `read_only` зробив би кожен старт
неможливим. Компенсувальні заходи: non-root uid `10001`, `cap_drop: ALL`,
`no-new-privileges`, tmpfs для `/tmp`, вимкнений auto-upload і жодного
published port.

## Ефективна конфігурація движка (P7)

Джерело істини — `core/engine_config.py`. Воно не додає другого механізму
перемикання: вибір, збереження, застосування і відкат лишаються в
`core/provider_switch.py` (Issue #78). P7 додає те, чого перемикач не знав:

- `effective_engine_config()` — знімок того, що ефективно зараз:
  `selected`, `effective`, `config_revision`, `agree`, endpoint identity,
  **посилання** на secret, `ready`, `reason`;
- `verify_effective_engine(probe=True)` — перевірка реального executor-а: для
  зовнішнього движка матриця і вибір не є доказом, готовим лише probe;
- `engine_config_revision(engine)` — стабільний digest несекретної
  конфігурації, який записується в snapshot кожної assembly-попытки.

`GET /api/settings/video-engine` віддає цей знімок. Значення секрету ніколи не
потрапляє у відповідь, у snapshot чи в digest — лише ім'я env-змінної і ознака
`configured`. Endpoint identity містить лише scheme/host/port: userinfo, шлях,
query і fragment відкидаються.

Перемикання на зовнішній движок приймається лише після probe на реальному
runtime; якщо движок не довів готовність, попередній ефективний движок і
config_revision зберігаються, а новий вибір не персиститься. Snapshot спроби
описує саме той engine, якому attempt було передано (`_engine_snapshot(engine=…)`),
і для зовнішнього движка несе `endpoint_reference` та `secret_reference`.

Перевірка:

```bash
python -m pytest tests/test_engine_configuration.py tests/test_assembly_worker_route.py -q
```

## Перевірки

```bash
python -m pytest tests/test_moneyprinter_runtime.py -q
python scripts/qualify-release.py
python scripts/moneyprinter-runtime-check.py --evidence moneyprinter-runtime-evidence
```

`qualify-release.py` додатково перевіряє, що `moneyprinter` є в `REQUIRED_IMAGES`
й у release matrix, але відсутній у `config/node_roles.json`. Кожен gate має
негативний тест у `tests/test_moneyprinter_runtime.py`, який ламає відповідну
властивість і вимагає, щоб gate відпав.

`moneyprinter-runtime-check.py` — єдина перевірка, яка реально збирає й
запускає образ. Вона читає pin-и з самого `Dockerfile`, збирає image, бере
immutable digest, стартує одноразовий контейнер без опублікованих портів і
вимагає від `/health`, `/runtime`, `/self-test` і `/sbom`: pinned commit,
bridge schema, збіг digest із реально збудованим образом, інвентар залежностей
і медіапайплайн, який справді створив файл. Далі перевіряє, що
`upload_post_*` лишається вимкненим у відрендереному `config.toml`.
Контейнер, образ і тимчасовий файл ключа видаляються завжди. Job CI
`.github/workflows/ci.yml` запускає цю перевірку йвантажить докази як
artifact.

## Статус перевірки

Перевірено без запуску контейнера (статичний аналіз pinned upstream):

- tarball `2e1b303…` завантажено і його SHA-256 збігається з
  `MPT_TARBALL_SHA256` у `Dockerfile`;
- `TaskVideoRequest` оголошує всі обов'язкові поля `REQUIRED_SUBMIT_FIELDS`,
  а `VideoAspect.portrait` / `VideoConcatMode.sequential` збігаються з bridge;
- `combine_videos` має сигнатуру, яку очікує короткий media pipeline;
- `app/config/config.py` пише `config.toml` у корінь застосунку, тому
  `/opt/moneyprinter` мусить лишатися writable;
- `faster_whisper` і `twelvelabs` імпортуються під `try`/lazily, тому
  `requirements.lock` їх виключає; усі інші імпорти `app/` покриті lock-ом;
- усі версії з `requirements.lock` існують на PyPI;
- pinned upstream приймає pre-rendered voice лише з task-каталогу:
  `resolve_custom_audio_file` дозволяє тільки такий шлях, а wrapper доставляє
  схвалений голос через `POST /api/v1/voice` і `_stage_task_voice()` (§9.3 рядок 5).
  Власний TTS upstream-а (`POST /api/v1/audio`) не використовується, тому
  `/runtime` повідомляє `task_local_voice: true`, а render без доведеного
  staging відхиляється pre-dispatch з `upstream_voice_staging_unsupported` (§9.6).

Не перевірено локально і потребує середовища з Docker: реальна збірка образу,
старт контейнера та фактичний compose render. Скрипт
`moneyprinter-runtime-check.py` виконує це в CI; локальний запуск на машині без
Docker завершується помилкою `docker is not available on this host`.
