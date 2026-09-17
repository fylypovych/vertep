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

Production `backup-service` отримує `BACKUP_ROOT=/data/backups` через
`deploy/docker-compose.yml`, який входить у runtime bundle для інсталяції та
оновлення. Bind mount `./backups:/data/backups` зберігає snapshot і receipt у
`/opt/vertep/backups` за стандартного installation root. Root filesystem залишається
`read_only: true`; `/health` перевіряє фактичний запис тимчасового файла та повертає
HTTP 503, якщо каталог неможливо створити або використати для запису.
Ключ читається з `/run/secrets/backup_encryption_key`.

Міграція наявних snapshot у host-каталозі не потрібна: штатне оновлення має
застосувати новий Compose та пересоздати контейнер. Самого restart зі старою
конфігурацією недостатньо. Копії з нестандартного старого `BACKUP_ROOT` потрібно
окремо перенести зі збереженням receipt перед видаленням старого контейнера.

Аудит writable paths: snapshot і тимчасові файли restore використовують
`BACKUP_ROOT`; відновлення джерел пише в змонтовані `/data/config` і `/data/storage`.
PostgreSQL dump використовує `/tmp/vertep.dump`, для якого вже налаштовано writable
tmpfs `/tmp` (128 MiB). Більший dump потребує окремого перегляду цього ліміту.
Виявлено окрему наявну проблему Redis restore: без `REDIS_DATA_DIR` він намагається
писати в `/app/var/lib/redis`, а локальний Redis data volume у Backup Service
не змонтовано. Це виправлення каталогу snapshot не вирішує підключення Redis data
для backup/restore. Довільні `BACKUP_*_CMD` і `BACKUP_SOURCES` також мають
використовувати доступні mounts.

Штатний сценарій приймання: backup → контрольована зміна/видалення даних → restore → verified health (файли, записи БД, jobs/history/receipts, checksums). Успішний mock restore доказом не є.

## Зв'язки

- Pre-update backup — частина `update-system.md`.
- Користувацькі `characters/brands/workflows` переносяться зі старого ephemeral root до його заміни; видалені користувачем записи seed не повертає.
