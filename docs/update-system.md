# Safe Update System

> Статус: чинний | Аудиторія: оператор, розробник | Канон: `AGENTS.md` §11

## Джерело

Канон — підписані GitHub Releases цього репозиторію (див. `release-contract.md`). Запуск: автоперевірка кожні 6 годин (systemd timer), Web UI (**Settings → Update → Install**) або CLI `vertep update`.

## Фази

```text
MAINTENANCE → DRAIN (workers завершують, звітують FREE) → Backup →
Download → Update (контейнери, міграції, рестарт) → Health Check → READY
```

- Перед оновленням — `MAINTENANCE`: диспетчер не видає нових задач, активні добігають, нові отримують `WAITING_FOR_SYSTEM`.
- Backup перед оновленням: конфігурація, PostgreSQL, Redis, секрети, ліцензії, журнали міграцій → `/opt/vertep/backups/update-<stamp>/`.
- Health Check: PostgreSQL, Redis, API, Web UI, Ollama, Dispatcher, GPU/Driver/CUDA, ComfyUI, VRAM + self-test кожного Worker. Успіх усіх — `READY`.

## Rollback

Будь-який провал health check — автоматичний rollback: попередні версія, контейнери, БД, конфігурація. Ручний: `vertep rollback`. Помилка release workflow сама по собі версій не створює і історії не переписує.

## Rolling Update

One-node-at-a-time, порядки `workers-first` / `core-first` / `custom`; canary: перший вузол → self-test → promote/rollback; автоматичний rollback при таймауті/помилці. Координатор — PostgreSQL-backed з global fencing.

## Web Update Center і CLI

Web UI показує current → target, changelog, фази, progress і фінальний persisted-результат (переживає рестарт). API приймає лише фіксовані дії (`check`, `update`, `restart`) — без довільних команд, URL чи гілок; Docker-сокет у CORE не прокидається. CLI: `vertep status|start|stop|restart|update|recover|rollback`.

## Логування

`/data/config/update/status.json`, `/data/config/update/log/` — кожен запис: час, фаза, статус, повідомлення.
