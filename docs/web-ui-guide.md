# Web UI — гід

> Статус: чинний | Аудиторія: оператор | Канон: `AGENTS.md` §4.5

Активна адмінпанель — `web-v2` (Angular, українська). Legacy `web/admin-uk.*` — застарілі, лишаються до доведеного parity.

## Розділи

- **Jobs** — список, фільтри, створення; картка Job: timeline подій (зі структурованим контекстом attempt/worker/task/artifact/error), previews, артефакти з verification, історія версій.
- **Queue** — черги, lease, dead-letter з ручним retry.
- **Fleet** — вузли: planned/installed/healthy, capabilities, метрики, remote controls (restart/update з видимим ефектом).
- **Dashboard** — KPI і реальні метрики; порожні стани чесні.
- **Channels** — readiness платформ, receipts публікацій.
- **System** — захищені інтеграції, моделі, провайдери, оновлення, backup, безпека, сертифікати, alerts/logs/health.

## Дії Job

Pause / Resume / Retry / Regenerate / Cancel / Delete / Approve / Publish — кожна прив'язана до дозволених станів, прав і in-flight запиту; недоступна дія пояснює причину (стан системи, роль, конфлікт версій 409).

## Review-цикли

Script → storyboard (сцени, промпти, версії) → voice/video previews → video revision/approval → publish. Усі мутації показують loading/empty/error і завершення skeleton; дати — у форматі дд.мм.рррр гг:хх; навігація — клавіатурою, діалоги — з focus/Escape.

> Деталі окремих flows (storyboard, video review) і відомі прогалини — у відкритих `i`/`ir` Issues.
