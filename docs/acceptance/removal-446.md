# Видалення Vertep — реалізація й докази #446

> Статус: локальна реалізація; повне приймання сервера NOT_RUN | Аудиторія: власник, розробник, ревізор | Канон: [AGENTS.md](../../AGENTS.md); [контракт команди](../removal.md).

Власник погодив концепцію дорученням «виуонуй» після пропозиції контракту в чаті 2026-11-11. Це дозвіл на реалізацію й ізольовані перевірки, а не запуск видалення чинного сервера або публікацію. База HEAD `0cfbf961e714ed35ae368073361dd82ce5909d7d`; результати стосуються dirty worktree з попередніми змінами, не чистого commit.

## Зміни

- `core/removal.py`: inventory, перевірка меж root, точний перелік/hash, збереження/окремий purge даних, shared-resource guards, update fence, readiness, journal і самодостатнє продовження.
- `scripts/vertep`, `scripts/remove-vertep.py`: команда remove до shell sourcing `.env`, без виконання конфігурації під час видалення.
- `bootstrap.sh`, runtime sync і обидва bundle builders: доставка executor та запис ownership inventory для нових runtime.
- `core/system_state.py`: admission marker блокує dispatch/create jobs під час видалення.
- Тести — `tests/test_removal.py`, `tests/test_removal_docker.py`; чинні system-state/update/release тести перевіряють суміжні контракти.

## Покриття контракту

| Вимога | Статус та доказ |
| --- | --- |
| Видалення ПЗ й units конкретного root | PASS на ізольованому filesystem/модельованому systemd; root/файли/units прив’язані до inventory |
| Дані зберігаються; purge окремо підтверджується | PASS: default/purge тести; підтвердження переліку; відмова при зміні scope |
| Сторонні ресурси не видаляються | PASS: однаковий project name іншого root, shared image/network/volume, foreign bind overlaps, symlink escape і drop-ins |
| Jobs, queued update/deployment, незупинений executor | PASS: відмова до stop/delete; live CORE readiness і admission marker; оновлення NORMAL не знімає marker |
| Повторний запуск і переривання | PASS: fault injection між resource-кроками, resume з тим самим переліком, відсутні ресурси та повторне виконання |
| Реальні Docker resources | PASS: два disposable сценарії default/purge; справжні volume/network/container видалення; сторонній контейнер лишається Running і спільний image збережено |
| Спільні пакети, repositories та глобальні налаштування | ЗБЕРІГАЮТЬСЯ: походження/використання не доведено, автоматичне видалення не реалізоване. Це явна межа, не припущення про належність |
| Старі/legacy інсталяції без ownership inventory | BLOCKED для автоматичного видалення; невідомі файли/пакети не привласнюються новим inventory за назвою |
| Повна Ubuntu/systemd деінсталяція, reboot і нова інсталяція | NOT_RUN: потрібен disposable server; Docker Desktop і simulated systemd не замінюють цей доказ |

## Перевірки та межі

Автоматизовані перевірки виконуються на Windows/Python 3.10 з Linux Docker daemon. Unit filesystem тимчасовий, OS/Docker boundary моделюється явно. `test_removal_docker.py` створює й прибирає лише власні одноразові Docker resources; systemctl у цьому тесті моделюється; перелік контейнерів обмежено унікальною label власного тестового стенда для ізоляції паралельних запусків. Жоден поточний Vertep server не видалявся.

JUnit `.local/issue-446-removal.xml` містить тестові IDs, результати та skips. OpenSSL/Bash-залежні skips суміжних release/update тестів не є PASS відповідних криптографічних/host-критеріїв. Bash syntax окремо перевіряється у контейнері зі read-only mount коду. Точні результати фінального запуску наведено в підсумку виконання.

Публікація коду, створення implementation/real-server Issues та закриття #446 не виконувалися.

Фінальна перевірка 2026-11-11: **246 passed, 8 skipped** у removal/system-state/update-security/release suite; два Docker-тести пройшли без skip. Один суміжний skip потребує OpenSSL, сім — Bash/release-layout середовища. Окремі `bash -n` для `bootstrap.sh` і `scripts/vertep` пройшли в `bash:5.2` зі read-only mount, `compileall`, локальні посилання та `git diff --check` — PASS. Повний Ubuntu/systemd/reboot/reinstall залишається NOT_RUN.
