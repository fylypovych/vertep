# Вузли та capabilities

> Статус: чинний | Аудиторія: розробник, оператор | Канон: [AGENTS.md](../AGENTS.md) §3, §14–§15.

## Ролі

| Роль | Склад |
|---|---|
| `core` | CORE + API + Dispatcher + Scheduler + Redis + PostgreSQL + License Manager + Certificate Manager + міграції + proxy + Update Agent; точний список — каталог ролей. Publisher і Monitoring підключаються як додаткові ролі. |
| `gpu` | Worker + ComfyUI + CUDA Runtime + Update Agent. |
| `text` | Worker + Ollama + LLM Runtime + Update Agent. |
| `voice` | Worker + TTS Runtime + Voice Models + Update Agent. |
| `publisher` | Publisher Worker + платформи + Update Agent. |
| `backup` | Backup Service + Snapshot Manager + Archive Service + Update Agent. |
| `monitoring` | Grafana + Prometheus + Logs + Metrics + Update Agent. |

Локальні ролі оператор вибирає в **Налаштування → Ролі вузла**; запит застосовує deployment executor. `NODE_ADDITIONAL_ROLES` у `.env` — параметр runtime, не заміна підтвердження застосування. Перелік ролей і служб — [config/node_roles.json](../config/node_roles.json), застосований набір — `deployment-plan.json`; готовність вимірюється окремо.

## Capabilities

Приклади каталогу: `image_generation`, `image_upscale`, `controlnet`, `inpainting`, `video_generation`, `text_generation`, `speech_synthesis`, `publishing`. Назви task (`tts`, `publish`) та назви capabilities не тотожні; актуальні allowlists задає `config/node_roles.json`. Worker заявляє їх при реєстрації і підтверджує self-test; диспетчер враховує VRAM, readiness моделей і свіжість self-test. Непідтверджені capabilities в dispatch не беруть участі.

## Реєстрація Worker

1. Bootstrap запускає базові служби; роль вибирається у Wizard. Під час enrollment Worker готує CSR і ключі вузла.
2. У Wizard вводяться Core URL + Registration Token (`VT-XXXX-XXXX-XXXX`, TTL 15 хв, одноразовий; таблиця `node_registration_tokens`).
3. Worker шле `/api/nodes/register` (CSR, capabilities, hardware, `enrollment_id`).
4. Core видає JWT + Worker Secret + конфігурацію; можливий mTLS (`NODE_MTLS_REQUIRED=true`).
5. Worker проходить self-test, з'являється у Web UI.

`enrollment_id` — стабільний локальний ідентифікатор enroll-спроби, який Worker
зберігає у `pki/enrollment.id` і передає на кожній спробі. Одноразовий токен
згортається при першій успішній реєстрації: якщо відповідь Core загубилася,
повтор із тим самим `enrollment_id` для того самого `node_id` не повертає
`401`, а перевипускає свіжий secret/generation/сертифікат. Ключ зберігається
лише як HMAC, а чужий `node_id` або інший ключ не збігаються — тобто вигорілий
токен не стає способом зареєструвати будь-кого.

## Стан ролей

`config/node_roles.json` — це **декларація** контракту ролі. Фактичний стан
обчислює `core/role_runtime.py` і повертає `runtime_status` для кожної з семи
ролей (`READY / DEGRADED / OFFLINE / UNKNOWN`):

- локальна роль — з власних health-перевірок і, за наявності, з
  `config/runtime-inventory.json`;
- віддалені ролі — з durable self-test (`ONLINE/OFFLINE`) і свіжості heartbeat.

`runtime_status` входить у `GET /api/system/roles` (поле `role_runtime_status`
та `runtime_status`/`runtime_evidence` у `available_roles`), відображається в
Web UI (Налаштування → Ролі) і враховується диспетчером: локально розгорнута
роль зі статусом `OFFLINE`/`DEGRADED` більше не приймає задачі.

## Спостереження і керування

- Heartbeat кожні 30 с; Core визначає `ONLINE / BUSY / FREE / UPDATING / OFFLINE / ERROR`.
- Remote controls: restart/update з підтвердженням виконання (ack/timeout/error), а не лише зміною бажаного стану.
- `rotate` (admin action) інварідує поточні облікові дані вузла: secret,
  JWT generation і сертифікат скидаються, старий serial потрапляє у CRL, а
  вузол відновлюється через `POST /api/nodes/{node_id}/renew` зі своїм CSR.
- Outbound-only топологія підтримується: Worker без inbound-портів працює з CORE і відновлюється після обриву мережі/reboot.
  Публікація inbound-порту для будь-якої non-Core ролі ламає цю модель, тому
  `core/outbound_only.py` механічно перевіряє Compose-файли репозиторію
  (`tests/test_outbound_only.py`).

## Моделі Ollama

- **Встановлені моделі** — фактичний `/api/tags` локальної Ollama CORE і каталоги, які Text-вузли передають у heartbeat.
- **Каталог для завантаження** (#454) — `core/ollama_catalog.json` у складі релізу, `GET /api/system/models/catalog`. Це перелік тегів бібліотеки Ollama для генерації тексту; кожен тег і його розмір завантаження звірено з `registry.ollama.ai`. Каталог не гарантує, що ресурсів вузла вистачить. Перед релізом зі зміненим каталогом запустити `python scripts/verify-ollama-catalog.py`: відсутній тег або змінений розмір дають код 1. Недоступний чи пошкоджений файл каталогу дає 503 `model_catalog_unavailable`, а не підставлений список.
- Вибраний з каталогу або введений вручну тег передається в чинний `POST /api/system/models/pull` разом із цільовим вузлом. Пробіли на краях назви UI прибирає; назва з пробілами чи командою `ollama pull …` відхиляється до запиту. UI окремо показує неправильну назву, відсутній у бібліотеці тег, недоступну Ollama та недоступний вузол. Повторне завантаження запускає той самий pull, а невдале завантаження не додає модель до встановлених.

## Голоси TTS

- TTS runtime (`services/tts_service.py`, espeak-ng) повертає в `GET /voices` голоси як `{"id": "uk", "name": "Ukrainian"}`: `id` — мовний код espeak-ng для синтезу, `name` — його VoiceName. `GET /api/models/voices` віддає цей самий контракт `{id, name}`, а також `speed_range` і `speed_unit: words_per_minute` (#455).
- `default` — голос рушія за замовчуванням: runtime синтезує без `-v`. Це не запис каталогу, тому dispatch завдань і далі звіряє лише явно заданий голос персонажа. Цей самий `default` використовує self-test ролі Voice.
- Швидкість — параметр espeak-ng `-s`, слова за хвилину; межі задає runtime (зараз 80–450), і CORE перевіряє preview саме за ними. Помилки preview мають коди (`unknown_voice`, `speed_is_outside_the_range_of_the_tts_runtime`, `tts_runtime_is_unreachable`, `voice_preview_failed`) без внутрішніх адрес; UI читає JSON-помилку і з відповіді-аудіо (Blob).
