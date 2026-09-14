# Vertep — карта документації

> Статус: чинний | Аудиторія: усі

Нормативні правила роботи агентів — `AGENTS.md` у корені репозиторію. Цей каталог — повний людський виклад системи українською. Там, де docs і `AGENTS.md` перетинаються, каноном є `AGENTS.md` (посилання `Канон` під заголовком).

## Порядок читання

1. `overview.md` — що таке Vertep і як працює конвеєр.
2. `glossary.md` — терміни (job, task, capability, provider…).
3. `architecture.md` — CORE, Worker, вузли, потоки даних.
4. Далі за роллю — оператор чи розробник.

## Оператору

- `installation.md` — встановлення: Bootstrap, ролі вузлів, First Run Wizard.
- `update-system.md` — безпечні оновлення, rollback, rolling/canary.
- `backup-restore.md` — резервні копії та відновлення.
- `security.md` — секрети, сертифікати, доступ.
- `monitoring.md` — health, alerts, logs.
- `web-ui-guide.md` — робота в адмінпанелі.
- `telegram.md` — керування через Telegram-бота.

## Розробнику

- `job-lifecycle.md` — стани Job і системні стани.
- `nodes-and-capabilities.md` — ролі, реєстрація, capabilities, raiting диспетчера.
- `providers.md` — інтерфейси движків і матриця backend.
- `content-pipeline.md` — сценарій → storyboard → голос → відео → монтаж.
- `characters-and-workflows.md` — формат персонажів і ComfyUI-workflow.
- `publishing.md` — Publisher-адаптери, receipts, правила публікації.
- `testing.md` — тести, gates, CI.

## Релізи та довіра

- `versioning.md` — нумерація, `пуш`/`реліз` (вказівник на `AGENTS.md` §3).
- `release-contract.md` — контракт GitHub Release та артефакти.
- `release-key-ceremony.md` — процедура root/release ключів.
- `core-generation-gate.md` — заборона генерації в CORE.
- `open-source-audit.md` — дослідження архітектури (історичне, частково реалізовано).
- `deployment-security-audit.md` — аудит безпеки розгортання.
- `requirements-gap-analysis.md` — історичний аналіз прогалин (архів; актуальні задачі — лише в GitHub Issues).
- `large-files-refactoring.md` — нотатка про завершений рефакторинг `0.0.1.12`.

## Конвенції каталогу

- Тільки файли першого рівня, без підкаталогів.
- Імена файлів — англійською, lowercase-hyphen; вміст — українською.
- Кожен файл має шапку `Статус / Аудиторія / Канон`.
- Заборонено файли планів, roadmap і TODO — черга робіт ведеться виключно в GitHub Issues (`AGENTS.md` §29.1).
