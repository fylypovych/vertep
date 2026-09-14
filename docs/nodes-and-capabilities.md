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
3. Worker шле `/api/nodes/register` (CSR, capabilities, hardware).
4. Core видає JWT + Worker Secret + конфігурацію; можливий mTLS (`NODE_MTLS_REQUIRED=true`).
5. Worker проходить self-test, з'являється у Web UI.

## Спостереження і керування

- Heartbeat кожні 30 с; Core визначає `ONLINE / BUSY / FREE / UPDATING / OFFLINE / ERROR`.
- Remote controls: restart/update з підтвердженням виконання (ack/timeout/error), а не лише зміною бажаного стану.
- Outbound-only топологія підтримується: Worker без inbound-портів працює з CORE і відновлюється після обриву мережі/reboot.
