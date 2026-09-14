# Backup / Restore

> Статус: чинний | Аудиторія: оператор | Канон: `AGENTS.md` §11.5

## Що зберігається

Повний склад: PostgreSQL (обов'язковий dump — пропуск dump не може завершитися успіхом), Redis, jobs/artifacts, персонажі, бренди, workflow, конфігурація, encrypted secrets, потрібний runtime state. Inventory архіву підтверджує фактичний склад включно з БД. Порожні каталоги відновлюються як порожні.

## Операції

- Створення, список (ID, час, тип, розмір, checksum/integrity), прогрес — через Web UI (**System → Zero-Shell lifecycle**) або Telegram-меню `Система` (див. `telegram.md`).
- Restore: вибір snapshot → підтвердження → maintenance/recovery state → відновлення точного складу → **обов'язкова post-restore health verification**. Без успішного health система в `NORMAL` не повертається.
- Restore поверх активних записів у `NORMAL`/`UPDATING` заборонено; стан контролюється протягом усієї операції.
- Помилка — контрольований перехід у безпечний системний стан з реальною зміною стану (не імітацією виклику).
- Remote copy: результат перевіряється, помилки політики сховища показуються; receipt чесний.

## Перевірка відновлення

Штатний сценарій приймання: backup → контрольована зміна/видалення даних → restore → verified health (файли, записи БД, jobs/history/receipts, checksums). Успішний mock restore доказом не є.

## Зв'язки

- Pre-update backup — частина `update-system.md`.
- Користувацькі `characters/brands/workflows` переносяться зі старого ephemeral root до його заміни; видалені користувачем записи seed не повертає.
