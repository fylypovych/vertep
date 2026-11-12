# Видалення інсталяції Vertep

> Статус: локальна реалізація; Ubuntu/systemd qualification окрема | Аудиторія: оператор, розробник | Канон: [AGENTS.md](../AGENTS.md) §0.4; погоджений контракт #446.

## Обсяг

`sudo vertep remove` видаляє лише підтверджені ресурси поточної інсталяції. Підтримується нестандартний installation root: wrapper визначає його від власного розташування; Python executor також приймає явний `--root`. Команда доступна в новому runtime bundle; старий встановлений CLI сам собою не оновлюється від локальної зміни репозиторію.

Перед переглядом чи видаленням потрібен `config/removal-ownership.json`, записаний інсталятором із переліку файлів runtime. Bootstrap реєструє inventory після отримання підписаного runtime та оновлює його після встановлення служб; синхронізація host runtime під час update також оновлює inventory. Файли, units, Docker image IDs, networks і volumes зберігаються з installation identity. Зміна зафіксованого файла або unit блокує автоматичне видалення.

Стара інсталяція без inventory, legacy/source installer без manifest файлів, невідомі зовнішні каталоги ComfyUI/Ollama або змінені systemd drop-ins не вважаються автоматично належними Vertep. Потрібна окрема інвентаризація й рішення оператора; команда не відновлює походження пакетів чи файлів за їхніми назвами.

## Перегляд і підтвердження

Наведені команди — інструкція оператору; документація не є дозволом запускати видалення на сервері.

```bash
sudo vertep remove --dry-run
```

JSON містить root, installation ID, точні container/network/image IDs, units, файли, дані, збережені й заблоковані ресурси та `plan_sha256`. Перегляд виконує лише читання. Значення secrets не друкуються; environment-файли не виконуються як shell-код.

Після перевірки переліку оператор запускає `sudo vertep remove` і вводить повний hash переліку. Для non-interactive запуску передається `--confirm <plan_sha256>`. Зміна переліку до виконання робить попереднє підтвердження недійсним. Приймання задач потрібно призупинити штатним maintenance/drain у CORE; активні jobs, Worker tasks, update або deployment блокують видалення. Активний non-Core/legacy runtime спочатку штатно зупиняється після завершення задач.

## Дані та спільне ПЗ

За замовчуванням зберігаються `config`, `tls`, `data`, `storage`, `models`, `logs`, `backups`, `.env`, `.update-backup.key` та Docker volumes. Це включає секрети й ключі, необхідні для використання encrypted backups. Залишені шляхи показуються у переліку; видалені systemd triggers і CLI не забезпечують автозапуску.

`--purge-data` — окремий явний вибір. Спочатку переглянути `sudo vertep remove --purge-data --dry-run`, потім підтвердити hash саме цього переліку. Видаляються лише перелічені локальні файли й підтверджені неспільні volumes. Перетини зі сторонніми bind mounts, symlinks і зміна даних блокують операцію. Спільні Docker resources не видаляються; journal та recovery executor зберігаються навіть у purge-режимі.

Після purge executor прибирає порожні каталоги даних, lock-файл update fence і сам installation root, якщо він став порожнім. Невідомий вміст не видаляється: його шляхи наводяться в `retained_unknown`, а результат — PARTIAL.

## Походження Docker images

Видаляються лише images, які до хоста приніс Vertep. Перший запис ownership inventory, зроблений bootstrap до першого pull і до створення будь-якого контейнера цього root, фіксує всі images, що вже були на хості (`image_provenance.preexisting`). Такі images — зокрема `postgres`, `redis`, `nginx`, prometheus чи grafana, завантажені оператором раніше — ніколи не видаляються, навіть коли їх використовували контейнери Vertep. Images, які використовує сторонній контейнер, також зберігаються.

Якщо inventory створено вже для працюючої інсталяції (старі версії без запису походження), походження невідоме. Тоді видаляються лише images, чий `repository@digest` збігається з підписаним `images.json` поточного чи збереженого release; решта зберігається з причиною у переліку.

Пакети Docker/Compose, драйвери, Toolkit, Ollama, apt repositories, Docker daemon settings, UFW/SSH зберігаються. Поточні інсталятори не мають повного журналу їхнього походження й попереднього стану; обґрунтувати автоматичне видалення або rollback цих змін неможливо. Опції обходу цієї перевірки немає. Їх видалення потребує окремих перевірених доказів і явного рішення оператора поза автоматичним executor.

## Належність та виконання

Compose project name `vertep` не є достатнім доказом. Перевіряються working directory/config files під потрібним root, resource inventory, перетини з усіма сторонніми контейнерами та фактичні посилання. Images видаляються без force; network/volume removal не обходить перевірку використання Docker. Global prune/autoremove не застосовуються.

Після підтвердження executor бере спільний з update/deployment OS lock, перевіряє live readiness, створює admission marker, повторно перевіряє drain, вимикає units/triggers, видаляє ресурси й лише потім — код і CLI. Marker не дозволяє відновити dispatch паралельною зміною NORMAL. Невідомі файли не видаляються рекурсивно: непорожні software-каталоги залишаються й дають PARTIAL.

## Переривання та повторний запуск

Journal `state.json` і самодостатній `remove.py` зберігаються поза installation root у приватному каталозі `/var/lib/vertep-remove/<installation-and-plan-key>/`. Path виводиться після завершення або помилки; journal не містить значень secrets. Кожен завершений крок записується атомарно. При помилці результат FAILED, units не вмикаються назад і дані не домальовуються як видалені.

Поки CLI ще встановлений, `sudo vertep remove --resume --confirm <original-plan-sha256>` сам знаходить єдиний незавершений journal цього root у `/var/lib/vertep-remove`; кілька незавершених journals або їх відсутність — відмова без змін. Після видалення CLI оператор запускає **збережений** executor: `sudo python3 /var/lib/vertep-remove/<key>/remove.py --root <installation-root> --resume --confirm <original-plan-sha256>`. Він використовує підтверджений перелік із journal і не додає нових цілей. Повторне виконання пропускає завершені кроки; відсутність уже видаленого ресурсу не є помилкою. Новий installation ID або новий перелік отримує окремий journal. Після переривання marker зберігає maintenance до завершення операції.

## Повторна інсталяція

Bootstrap на тому самому root після видалення без purge використовує збережені `config`, `.env`, дані й volumes як наявну інсталяцію: завершений `installation.json` не вимагає повторного майстра. Ownership inventory зберігає installation ID і запис походження images, а файли попереднього runtime, яких уже немає, з нього прибираються. Поки `config/removal-in-progress.json` існує, видалення не завершене: bootstrap відмовляє до будь-яких змін і пропонує продовжити видалення. Після purge root порожній або відсутній, тож bootstrap виконує чисту інсталяцію з майстром.

Докази й межі перевірок — [приймання #446](acceptance/removal-446.md).
