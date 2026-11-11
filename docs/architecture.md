# Архітектура

> Статус: чинний | Аудиторія: розробник, оператор | Канон: [AGENTS.md](../AGENTS.md) §3, §14.

## Топологія

```text
Telegram / Web UI (web-v2)
        │ HTTPS / polling
        ▼
CORE ── PostgreSQL, Redis
 │  API │ Dispatcher │ Scheduler │ Node Registry │ Update Agent
 │ capability-driven task dispatch (claim/result, lease, heartbeat)
 ▼
Text Node (Ollama/LLM) │ GPU Node (ComfyUI) │ Voice Node (TTS)
Publisher Node (платформи) │ Backup Node │ Monitoring Node (Prometheus/Grafana/Loki)
```

- Роль визначає, що **встановити**; capability — що вузол **вміє**; диспетчер шукає за capability.
- Один Worker може нести кілька capabilities; один job розпаралелюється сценами між воркерами.
- Production-сервіси працюють у Docker-контейнерах. Сховища визначає [production Compose](../deploy/docker-compose.yml): PostgreSQL/Redis використовують named volumes `postgres-data` / `redis-data`; конфігурація, logs, storage, backups і моделі Worker — bind mounts каталогів installation root. Ollama має окремий named volume `models`. Compose project може додавати префікс до фактичних назв volumes. Контейнери замінюються, користувацькі дані зберігаються поза їхнім тимчасовим filesystem.

## Потоки (спрощено)

```text
Сценарій:   CORE → task(script) → Text Worker → result → SCRIPT_PENDING_APPROVAL → approve
Зображення: CORE → task(image) → GPU Worker (ComfyUI prompt/history/view) → artifact → approval
Голос:      CORE → task(tts) → Voice Worker → перевірений audio-artifact
Монтаж:     CORE (AssemblyProvider/FFmpeg) → final/video.mp4 → VIDEO_PENDING_APPROVAL
Публікація: approve → task(publish) → Publisher Worker → receipt (remote ID/URL)
```

- CORE ніколи не виконує генерацію напряму (див. `core-generation-gate.md`, [AGENTS.md](../AGENTS.md) §3.1).
- Кожен перехід стану — через API/контракти, з event log, версіями і idempotency; повтори callbacks не створюють дублікатів ефектів.

## Сховища і стійкість

- PostgreSQL — jobs, history, registry, tokens; Redis — черги, lease, координація rolling update.
- `recover_after_restart()` відновлює jobs після рестарту CORE; heartbeat кожні 30 с; systemd — автозапуск; startup recovery — відновлення стану.
- Активні задачі переживають `MAINTENANCE`; нові в цей час отримують `WAITING_FOR_SYSTEM`.

## Конфігурація, а не код

- Персонажі — `characters/<id>/`; workflow — `workflows/`; ролі вузлів — `config/node_roles.json`; провайдери — `.env` + `providers.md`.
- Наявні ролі та capabilities вибираються конфігом. Нові поведінка, платформа або движок потребують відповідного адаптера/executor і погодженого scope; сам запис у конфігу не доводить готовності.
