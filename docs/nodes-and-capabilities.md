# Вузли та capabilities

> Статус: чинний | Аудиторія: розробник, оператор | Канон: `AGENTS.md` §7, §8, §14–§17

## Ролі

| Роль | Склад |
|---|---|
| `core` | CORE + API + Dispatcher + Scheduler + Publisher-контракти + Redis + PostgreSQL + License Manager + Update Agent + Monitoring. |
| `gpu` | Worker + ComfyUI + CUDA Runtime + Update Agent. |
| `text` | Worker + Ollama + LLM Runtime + Update Agent. |
| `voice` | Worker + TTS Runtime + Voice Models + Update Agent. |
| `publisher` | Publisher Worker + платформи + Update Agent. |
| `backup` | Backup Service + Snapshot Manager + Archive Service + Update Agent. |
| `monitoring` | Grafana + Prometheus + Logs + Metrics + Update Agent. |

Додаткові ролі Core — через `NODE_ADDITIONAL_ROLES` у `.env`. Перелік ролей для Wizard — `config/node_roles.json`.

## Capabilities

Приклади: `image_generation`, `image_upscale`, `controlnet`, `inpainting`, `video_generation`, `tts`, `publish_youtube`, `publish_tiktok`. Worker заявляє їх при реєстрації і підтверджує self-test; диспетчер враховує VRAM, readiness моделей і свіжість self-test. Непідтверджені capabilities в dispatch не беруть участі.

## Реєстрація Worker

1. Bootstrap ставить роль і генерує CSR.
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
