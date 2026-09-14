# Публікація

> Статус: чинний | Аудиторія: розробник, оператор | Канон: `AGENTS.md` §4.4

## Архітектура

Кожна платформа — окремий адаптер у `publishers/<platform>/`: `youtube`, `tiktok`, `facebook`, `instagram`, `threads` (+ Telegram). CORE передає готовий ролик і метадані; Publisher Worker виконує публікацію і повертає валідований receipt (платформа/канал, remote ID, URL, час, статус/помилка).

## Правила

- Публікується лише затверджена поточна video-версія; publish до approval заборонено на всіх шляхах (Web, Telegram, retry).
- Idempotency на рівні intent job/version/channel: повтор після часткового успіху не дублює успішні канали; в кожного каналу свій retry-budget.
- Transient/permanent/`NOT_CONFIGURED` помилки обробляються окремо; receipts перевіряються за схемою і прив'язкою до каналу/версії.
- Відео на окремий Worker потрапляє явним artifact-transfer контрактом з integrity-контролем, а не спільним випадковим filesystem.
- Секрети й токени не потрапляють у logs/DOM/receipts/metadata (перевіряється синтетичними маркерами).
- OAuth lifecycle (expiry/refresh/revoke/reconnect/scopes) — для кожної підтримуваної платформи; readiness видно в UI.

## Режими

- **Sandbox/test** — погоджені тестові акаунти або приватні режими; receipt перевіряється (remote ID, URL, видиме медіа).
- **Live** — лише за явним дозволом власника акаунта; жоден Issue чи документ такого дозволу не надає.

## Керування

Канали: readiness/config/test publish/result у Web UI; системні операції (див. `telegram.md`); публикація зі script approval — через штатні Job-дії Approve/Publish.
