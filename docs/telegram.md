# Telegram

> Статус: чинний | Аудиторія: оператор | Канон: `AGENTS.md` §6

## Підключення бота

Адмінка → **Система → Захищені інтеграції** → токен бота в `telegram_bot_token`; розділ **Telegram**: `TELEGRAM_WEBHOOK_SECRET` (опційно), `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ADMIN_CHAT_IDS`. Polling (`getUpdates` з durable offset) стартує з CORE; `PUBLIC_URL`/webhook для штатного flow не потрібні (legacy webhook вимкнений за замовчуванням).

## Контентний flow

Повідомлення боту → тема → вибір персонажа (або прямий Job, якщо бренди не налаштовано) → script revision/approval → storyboard/image revision/approval → voice/video → video revision/approval → вибір publication target → receipt/URL. Callbacks перевіряють chat/роль/доступ до job до обробки; повтори idempotent; stale-кнопки до нових версій не застосовуються; публікація до approval заборонена.

## Системні операції (розділ `Система`, лише admin-чати)

`Status` (версія, стан, health CORE/БД/воркерів, jobs, backup, update) • `Update` (current → target, changelog, confirmation, фази, rollback) • `Restart` (services/CORE/Worker/Node, з drain-перевіркою) • `Backup` (створити/список/progress) • `Restore` (двоетапне підтвердження, maintenance, post-restore health) • `Test` (Quick / Full Self-Test / Test Node: `OK / WARNING / ERROR / NOT CONFIGURED`).

Telegram — лише control surface: жодних shell-команд, тільки фіксовані backend-actions; довгі операції — з `operation_id`, прогресом і persisted-результатом; критичні — з підтвердженням; секрети в чат/логи не виводяться.
