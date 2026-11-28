# Safe Update System

> Статус: чинний | Аудиторія: оператор, розробник | Канон: [AGENTS.md](../AGENTS.md) §15.

## Джерело

Канон — підписані GitHub Releases цього репозиторію (див. `release-contract.md`). Запуск: автоперевірка кожні 6 годин (systemd timer), Web UI (**Settings → Update → Install**) або CLI `vertep update`.

## Фази

```text
MAINTENANCE → DRAIN (workers завершують, звітують FREE) → Backup →
Download → Update (контейнери, міграції, рестарт) → Health Check → READY
```

- Перед оновленням — `MAINTENANCE`: диспетчер не видає нових задач, активні добігають, нові отримують `WAITING_FOR_SYSTEM`.
- Backup перед оновленням: конфігурація, PostgreSQL, Redis, секрети, ліцензії, журнали міграцій → `/opt/vertep/backups/update-<stamp>/`.
- Health Check залежить від застосованих ролей та backend: CORE/БД/Redis, вибрані службові залежності й role-specific self-test Worker. Ollama, GPU/CUDA/ComfyUI та інші невибрані залежності не роблять CORE несправним. Потрібна вибрана залежність із відмовою блокує готовність. Контракт — [core/health_checks.py](../core/health_checks.py) і [runtime readiness](runtime-readiness.md).

## Rollback

Будь-який провал health check — автоматичний rollback: попередні версія, контейнери, БД, конфігурація. Ручний: `vertep rollback`. Помилка release workflow сама по собі версій не створює і історії не переписує.

Окремо від CORE можна перезапустити, оновити чи відкотити панель: `docker compose up -d web-ui` з іншим `VERTEP_WEB_UI_IMAGE` (#504). API і фонові задачі CORE під час цього працюють; панель після оновлення одразу завантажує нову збірку (shell не кешується).

## Rolling Update

One-node-at-a-time, порядки `workers-first` / `core-first` / `custom`; canary: перший вузол → self-test → promote/rollback; автоматичний rollback при таймауті/помилці. Координатор — PostgreSQL-backed з global fencing.

## Web Update Center і CLI

Web UI показує current → target, changelog, фази, progress і фінальний persisted-результат (переживає рестарт). API приймає лише фіксовані дії (`check`, `update`, `restart`) — без довільних команд, URL чи гілок; Docker-сокет у CORE не прокидається. CLI: `vertep status|start|stop|restart|update|recover|rollback`.

## Логування

`/data/config/update/status.json`, `/data/config/update/log/` — кожен запис: час, фаза, статус, повідомлення. Події модульного `UpdateManager` записуються у структурований журнал `logs/update.jsonl` під категорією `UPDATE` з прив'язкою до `operation_id`.

## Незалежні компонентні релізи (#509/#510)

- Реєстр компонентів — `config/components.json`: CORE зберігає спільну послідовність `VERSION` і runtime-реліз (`реліз`), а офіційні модулі версіонуються й випускаються незалежно з тегами `<component>.<version>` (`A.B.C.D`, перенесення після 99).
- `scripts/component-release.py` і workflow `.github/workflows/component-release.yml` класифікують зміни (`impact`), запускають потрібні тести (модульні, сумісності або повний набір при невідомому впливі чи змінах CORE), планують версії (`plan`), збирають digest-закріплені образи, підписують маніфести (RSA) та модульні пакети `package.json` (Ed25519) і публікують стабільні релізи лише за `COMPONENT_RELEASES_ENABLED=true` із `--latest=false`.

## UpdateManager і джерела модулів (#511/#512)

- `ModuleSources` (`core/module/sources.py`) розв'язує, кешує та закріплює (pin) підписані пакети модулів із офіційного репозиторію або дозволених джерел із перевіркою довіри за `config/module-trust/publishers.json`.
- `UpdateManager` (`core/module/update_manager.py`) виконує лише затверджені адміністратором плани оновлення з конкретними версіями та вузлами (`PENDING_APPROVAL → APPROVED → RUNNING / WAITING_CORE → DONE / FAILED`):
  - якщо модуль вимагає новіший CORE, план спершу ініціює крок CORE через Update Agent і продовжується після перезапуску;
  - модулі оновлюються етапами за графом залежностей, а всередині одного модуля вузли отримують новий пакет послідовно через `RolloutGates` із перевірками `container`, `api`, `database` та `dependencies`;
  - при збої відкочується проблемний модуль і вже оновлені компоненти, що залежать від його нової версії.

## Snapshots, write fence, rollback і retention (#513/#517)

- `ModuleLifecycle` (`core/module/lifecycle.py`) знімає верифікований snapshot перед оновленням, закриває запис (write fence) на час операції та відкриває його лише після підтвердженої перевірки нової або відновленої версії (`_unlock_if_verified`).
- Адміністратор може відкотитися до верифікованих точок (`rollback_points`, `rollback_to_point`; відновлення БД вимагає явного підтвердження втрати пізніших записів). Retention `prune(module_id, keep=3)` зберігає активну і дві попередні успішні копії, не зачіпаючи активний запис і незавершений журнал.
- `scripts/prune-images.py` після успішного встановлення/оновлення фіксує версію й образи в `config/installed-versions.json` (`record`) і видаляє старі локальні образи Vertep поза останніми `keep` версіями (`prune`, типово 3), не чіпаючи активні контейнери та образи інших проєктів.

## Автономне A/B-відновлення Update Agent (#514)

- Update Agent працює з двох локальних верифікованих слотів `<root>/agent/slots/a` і `b` (`scripts/agent-slots.py`, стан у `<root>/agent/active.json`). Нова версія встановлюється в неактивний слот (`install`) і перемикається (`switch`) лише після локальної перевірки готовності.
- Хостовий watchdog (`scripts/watchdog.py::check_agent_slot`) виконує `scripts/agent-slots.py check`: якщо активний слот не проходить перевірку цілісності чи готовності, watchdog локально повертає попередній верифікований слот без залежності від доступності CORE.
