# Telegram

Set `TELEGRAM_BOT_TOKEN` (via Secure Integrations), під час старту CORE polling запускається автоматично. `PUBLIC_URL` та webhook не потрібні для штатної інсталяції.

Опціонально: `TELEGRAM_WEBHOOK_SECRET` для підпису, `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ADMIN_CHAT_IDS`, `TELEGRAM_POLLING_ENABLED` (default `true`), `TELEGRAM_POLLING_TIMEOUT` (default `30`), `TELEGRAM_POLLING_RETRY_DELAY` (default `5`), `TELEGRAM_POLLING_MAX_RETRY_DELAY` (default `300`).
