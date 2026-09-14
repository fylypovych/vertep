# Vertep

Завдання та стан розробки ведуться в [GitHub Issues](https://github.com/fylypovych/vertep/issues). Старі файли планів видалено після перенесення вимог; правила для агентів — у [AGENTS.md](AGENTS.md#29-робота-через-github-issues). Повний виклад системи українською — у каталозі [docs/](docs/README.md).

Vertep — модульний оркестратор фабрики контенту для Ubuntu Server 24.04. CORE володіє jobs і диспетчеризує GPU-роботу; WORKER виконують змінні ComfyUI-workflow і повертають артефакти; CORE збирає валідний MP4 через FFmpeg.

## Production installation

На чистому хості Ubuntu Server 24.04 виконати:

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

Команда від початку до кінця використовує цей самий публічний репозиторій `fylypovych/vertep`. Bootstrap перевіряє хост, встановлює Docker і виявлений NVIDIA/AMD runtime, завантажує останній підписаний GitHub Release цього репозиторію, перевіряє кожен runtime-файл, тягне digest-pinned образи з публічних GHCR-пакетів репозиторію, генерує credentials і TLS, стартує вибрані сервіси й чекає їх здоровими. Другий репозиторій чи зовнішній release-сервер не потрібні.

Якщо перше встановлення перервано або контейнер не пройшов healthcheck — виконати ту саму команду ще раз. Bootstrap відновлює існуючий appliance: перевикористовує PostgreSQL/Redis volumes, паролі, ключі шифрованого сховища, TLS/Node CA, вибрану роль, домен і локальні налаштування, замінюючи лише release-керовані файли й підписані посилання на образи. Вигадувати замінні credentials, коли persistent-дані є, а key-файла немає, — відмовляється. VM-снапшот чи перевстановлення Ubuntu не є штатною процедурою відновлення.

Після завершення відкрити надрукований адрес `https://SERVER-IP:8443` і пройти First Run Wizard. Подальше налаштування, бекапи, моделі, сертифікати, enrollment вузлів і підписані оновлення керуються через Web UI без повторного Bootstrap. Розробницьке встановлення з сирців — у розділі [Legacy/source installation](#legacysource-installation) нижче.

## Demo

```bash
cp .env.example .env
# Set unique ADMIN_PASSWORD, NODE_API_TOKEN and POSTGRES_PASSWORD values.
docker compose up --build
```

Відкрити `http://localhost:8080` і увійти як `ADMIN_USER` (дефолт `admin`). Demo-режим так само проходить CORE/WORKER task-протокол, але замість GPU-моделі використовує детерміноване зображення. Результат `jobs/<job_id>/final/video.mp4` — справжній MP4.

Для локального CORE-only запуску розробника — `LOCAL_WORKER_FALLBACK=true`. Цей fallback існує для розробки і вимкнений у постаченому Compose-оточенні; production-приймання з ним недійсне.

## Legacy/source installation

Для розробки або просунутих source-розгортань: `sudo ./install.sh`, далі вибрати `CORE`, `GPU WORKER` або обидва. Production-інсталяції мають використовувати підписаний [Production installation](#production-installation) потік. Source-інсталятор:

- перевіряє Ubuntu 24.04;
- встановлює Docker, Compose, Git, Python, FFmpeg і firewall-правила;
- встановлює Ollama для CORE;
- встановлює рекомендований NVIDIA driver, NVIDIA Container Toolkit і ComfyUI для WORKER;
- визначає модель, VRAM, температуру, навантаження і доступність CUDA;
- встановлює хостовий Web update watcher на CORE без доступу до Docker-сокета;
- пише `/etc/vertep/node.conf` і вмикає лише відповідні systemd-юніти.

Встановлення драйвера може вимагати reboot і повторного запуску інсталятора. Покласти GPU-сумісний checkpoint у `/opt/ComfyUI/models/checkpoints` і вказати його ім'я як `COMFYUI_CHECKPOINT`. Включений workflow API-формату працює зі стандартними Stable Diffusion checkpoints і замінюється під персонажа. Для карт Pascal інсталятор вибирає pinned CUDA 12.1/PyTorch-лінійку замість сліпого встановлення найновішої збірки.

### NVIDIA GeForce GTX 1660 (6 GB)

Worker-інсталятор розпізнає GTX 1660, GTX 1660 SUPER і GTX 1660 Ti як Turing (compute capability 7.5). Профіль 6 ГБ використовує pinned PyTorch 2.6.0 CUDA 12.4 wheels і стартує ComfyUI з `--lowvram`. Після встановлення перевіряє `torch.cuda.is_available()` перед увімкненням сервісу. ComfyUI слухає лише `127.0.0.1:8188`; окремий Worker дістається його через host networking.

Безпечним дефолтом лишається `SUPPORTED_TASKS=image`. Відеогенерація на 6 ГБ карті залежить від моделі і вмикається лише після тесту вибраного workflow на споживання VRAM. CORE диспетчеризує за живим звітом вільної VRAM Worker, тому `min_vram_mb` має бути нижчим за фактично доступне значення, а не номінальні 6144 МБ.

Пакети й сервіси інсталятора описані в `installer/manifest.json`. Додаткові ролі вузлів додаються як manifest-ролі/профілі; опційні ідемпотентні setup-хуки — в `installer/roles/`.

Окремий GPU-вузол використовує `docker-compose.worker.yml`; цей файл містить лише Worker і його log volume, тож випадково стартувати CORE, Redis чи PostgreSQL не може. Під час WORKER-only встановлення ввести токен, виданий/налаштований на CORE (`VERTEP_NODE_TOKEN` для unattended-встановлень). `CORE_ADDRESS` і `NODE_NAME` з `/etc/vertep/node.conf` перекривають шаблонні значення `.env`.

## Telegram

1. Відкрити адмінку → **Система → Захищені інтеграції** → вставити токен бота в `telegram_bot_token`.
2. Перейти до розділу **Telegram** і вказати:
   - `TELEGRAM_WEBHOOK_SECRET` — довільний секрет для підпису (не обов'язково)
   - `TELEGRAM_ALLOWED_CHAT_IDS` — дозволені Telegram `chat_id`, через кому
   - `TELEGRAM_ADMIN_CHAT_IDS` — адмінські чати для затвердження, через кому
3. Натиснути **Зберегти**. Polling запускається автоматично під час старту CORE.
4. Перевірити в Telegram: написати боту повідомлення. Якщо бренди не налаштовано, створюється Job без затвердження; якощо налаштовано — бот просить вибрати бренд.

Оновлення Telegram обробляються через long polling (`getUpdates` з постійним offset). Публічна URL-адреса (`PUBLIC_URL`) більше не потрібна для штатної інсталяції.

## Operations

```bash
vertep status
vertep start
vertep stop
vertep restart
vertep update
vertep recover
vertep rollback
```

Хелпери — у `scripts/vertep`. `update` читає встановлену роль вузла і стартує лише відповідні сервіси. Підписані релізи готуються в immutable release-директорії й активуються атомарно. Jobs та історії подій persist-яться у Job volume; Redis і PostgreSQL мають persistent volumes і restart policies.

На CORE порядок оновлення свідомо такий: drain навантаження, створення і перевірка бекапів, валідація підписаного релізу, перевірка вже запущених database-сервісів, застосування кожної незастосованої міграції/backfill, потім активація і health-check нових application-сервісів. PostgreSQL і Redis під час звичайного application-оновлення не перестворюються. Якщо база не стала ready або міграція впала — новий CORE не активується.

Кластерні оновлення використовують PostgreSQL-backed rolling-координатор з global fencing. Вузли дрейняться в детермінованому порядку, canary-розгортання вимагає явного promotion, а провалений health check запитує rollback кожного вузла до записаної попередньої версії. Відновлювані data backfills тримають durable checkpoints і продовжуються після переривання.

`vertep status` лишається придатним на Worker, поки CORE офлайн: локальна GPU-інформація все одно показується. Коли CORE досяжний, Worker отримує спільний системний статус через свій node-scoped токен замість пароля адміністратора.

### Signed updates in the Web UI

На встановленому CORE-вузлі відкрити **Система → Безпечне оновлення Vertep**. Спочатку вибрати **Перевірити оновлення**; кнопка **Встановити оновлення** вмикається лише коли підписаний update-сервіс повідомляє про новіший сумісний реліз. Встановлення йде асинхронно, тож сторінка може коротко втратити з'єднання під час активації й рестарту сервісів. Persistent-статус показує поточну й доступну версії, фазу оновлення, стан системи й останній update-лог.

Web API ніколи не отримує команду, URL репозиторію чи гілку. Він може ставити в чергу лише фіксовані maintenance-дії на кшталт `check`, `update` і `restart`; emergency recovery повертає систему в нормальний режим лише після проходження health checks CORE, PostgreSQL і Redis. Root-owned systemd path unit обробляє привілейовані запити на хості. CORE-контейнер Docker-сокета не отримує.

Web-оновлення вмикаються Bootstrap або Ubuntu CORE-інсталятором (`WEB_UPDATE_ENABLED=true`) і обмежені адміністраторами. Релізи тягнуться напряму з публічної GitHub Releases-стрічки `fylypovych/vertep`; кожен реліз містить окремо підписаний update manifest, що зв'язує версію, metadata сумісності й SHA-256 runtime-пакета. Updater входить у maintenance mode, дрейнить активну роботу, створює application/job/configuration/migration бекапи й PostgreSQL-бекапи, атомарно перемикає digest-pinned образи, потім застосовує пакет. Провалені health checks відновлюють попередній реліз і оточення; durable update-фаза дозволяє відновлення після втрати живлення. Systemd timer перевіряє релізи кожні шість годин. Встановленим вузлам GitHub credentials не потрібні.

Після оновлення старої інсталяції до релізу, що вперше містить Web updater, один раз виконати `sudo ./install.sh` для встановлення й увімкнення `vertep-update.path`. Якщо оновлення провалило health check — `vertep rollback` з консолі сервера; бекапи бази й остання відома Git-ревізія зберігаються в директорії проєкту.

## API overview

- `POST /api/jobs`, `GET /api/jobs`, `PATCH /api/jobs/{id}`
- `POST /api/jobs/{id}/pause|resume|retry|regenerate|cancel|approve|publish`
- `PUT /api/jobs/{id}/uploads/references/{filename}` (raw request body)
- `GET /api/jobs/{id}/artifacts`, `POST /api/jobs/{id}/artifacts/verify`
- `GET /api/jobs/{id}/export`, `POST /api/projects/import` (ZIP)
- `GET /api/tasks/dead-letter`, `POST /api/tasks/dead-letter/{task_id}/retry`
- `DELETE /api/jobs/{id}`
- `POST /api/tasks/claim`, `POST /api/tasks/result`
- `POST /api/workers/heartbeat`, `GET /api/workers`
- `POST /api/telegram/webhook`, `POST /api/telegram/setup`
- `GET /api/system/update`, `POST /api/system/update/check|run` (POST лише адміністратор)
- `GET|POST /api/system/backups`, `POST /api/system/backups/{snapshot_id}/restore`
- `GET /api/system/models`, `POST /api/system/models/pull`, `DELETE /api/system/models/{name}`
- `GET /api/system/certificates`, `POST /api/system/certificates/renew`
- `GET /api/system/license`, `GET /api/system/installation-manifest`
- `GET /api/status`, `GET /api/integrations`, `GET /api/health`

Машинні ендпоїнти можуть захищатися `NODE_API_TOKEN`; Web UI і адміністративний API використовують HTTP Basic authentication. Тримати `.env` локально — він виключений з Git.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

Тести покривають локальний end-to-end MP4 і справжній розподілений claim/result-контракт. Також покрито video-worker artifact-контракт: CORE приймає валідовані scene-кліпи, а FFmpeg конкатенує їх у фінальний MP4. Семпл `workflows/video/demo.json` цілиться в ComfyUI Video Helper Suite (`VHS_VideoCombine`), тож виконання цього workflow на реальному залізі вимагає відповідного custom node; його локальний registry і transport-контракт запущеного сервера не вимагають.

GitHub Actions ганяє повний suite, компіляцію Python, shell syntax checks і валідацію Compose-конфігурації на кожен push і pull request.

## Релізи та номери версій

Канон — `AGENTS.md` §3; виклад — `docs/versioning.md`, контракт артефактів — `docs/release-contract.md`.

Кожен реліз оформлюється одним версійним комітом у `main`. Назва коміту — лише номер версії формату `A.B.C.D` (послідовно: `…0.0.1.99 → 0.0.2.0`); той самий коміт вміщує код, оновлений `VERSION`, секцію `CHANGELOG.md` (`## ПРАВИЛЬНА НАЗВА: <версія>`) і файл `releases/<версія>.md` (`# Vertep <версія>`).

Два входи:

- агент-команда `пуш` (повний цикл за `AGENTS.md` §3.6: версія → файли → перевірки → один commit → push у `main`, без tag/Release);
- локально: змістовні українські пункти в секцію `Unreleased` файлу `CHANGELOG.md`, потім `python scripts/release.py` (визначає наступний номер, формує нотатки, запускає перевірки, створює один готовий коміт і відправляє в `main`).

Далі команда `реліз`: перевірка чистого дерева → запуск **Actions → Vertep Release → Run workflow**. Workflow `main` не змінює і другого коміту від бота не створює: перевіряє готовий коміт, збирає образи й підписані артефакти, ставить тег саме на цей коміт і публікує GitHub Release. Наступний номер — `python scripts/release.py --show-next`, аудит готового коміту — `python scripts/release.py --check`.

## Orchestration and artifacts

Кожен Job має явні стадії SCRIPT, ASSETS, TTS, ASSEMBLY і PUBLISH. Сцени сценарію диспетчеризуються незалежними tasks, тож один Job можуть паралельно обробляти кілька воркерів. CORE повторює лише провалену сцену; вичерпані tasks ідуть у dead-letter queue. Assembly стартує, коли кожна сцена досягла READY.

Щоб відкласти обробку, в `POST /api/jobs` встановити `scheduled_for` як ISO-8601 timestamp. Кожен згенерований чи завантажений файл записується в `manifest.json` з MIME-типом, розміром, SHA-256 digest, provenance сцена/task/worker/workflow. Перевірені завантаження відхиляють відсутні або модифіковані файли.

`PATCH /api/jobs/{id}` приймає `expected_version`; stale-значення повертає HTTP 409. Це не дає двом браузерним сесіям мовчки перезаписувати зміни одна одної.

CORE також надає priority/leased tasks з watchdog recovery, структуровані ротовані логи, Character і Brand APIs, багатосценовий FFmpeg assembly, Telegram-команди, per-worker токени, адміністративні сесії та mock-safe publisher-контракти. Живі завантаження в соцмережі й далі вимагають platform-specific API credentials та реалізацій.

`sudo ./install.sh --dry-run` — read-only preflight, `python scripts/generate-env.py` — створення унікальних локальних секретів, `python scripts/upgrade-config.py` — додавання нових ключів конфігурації після оновлень без перезапису існуючих. Поточні release-metadata — у `VERSION` і `CHANGELOG.md`.

## Provider layer (replaceable engines)

Повний виклад — `docs/providers.md`. Коротко: Vertep викликає кожен зовнішній движок через формальні інтерфейси в `adapters/providers/base.py` (`LLMProvider`, `ImageProvider`, `VideoProvider`, `TTSProvider`, `AssemblyProvider`, `ComputeProvider`, `PublisherProvider`, `VideoEngine`). Обгортки в `adapters/providers/__init__.py` дають registry (`providers.*`) і тримають дефолти взаємозамінними без дотику до Job Orchestrator. Опційні движки вмикаються лише явним opt-in через `.env`, інакше фабрики відкочуються на нативний backend:

- **LLM**: `ollama` (дефолт) або OpenAI-сумісний `openai` — `VERTEP_LLM_PROVIDER`.
- **TTS**: `none`/`mock` (дефолт), `piper` (MIT), `kokoro` (Apache-2.0) — `TTS_PROVIDER`.
- **Compute / GPU image-video**: `vertep-worker` (дефолт, приєднаний ComfyUI) або `comfyui-distributed` — `VERTEP_COMPUTE_PROVIDER`, `COMFYUI_DISTRIBUTED_URL`/`_TOKEN`.
- **Assembly**: нативний FFmpeg.
- **VideoEngine** (фінальний рендер): `native` (дефолт), `money-printer`, `shortgpt` — `VERTEP_VIDEO_ENGINE`, `MONEY_PRINTER_URL`/`_TOKEN`, `SHORTGPT_URL`/`_TOKEN`.
- **Publisher**: офіційні адаптери Telegram і YouTube/TikTok/Facebook/Instagram/Threads.

Активна backend-матриця — `provider_matrix()` в `/api/status` і Web UI **Settings → Engines (backends)**. Власником Job-циклу лишається Vertep; зовнішні движки лише рендерять або публікують.
## Appliance runtime details

На NVIDIA-хостах Bootstrap встановлює рекомендований driver і NVIDIA Container Toolkit, реєструє Docker runtime і перевіряє `nvidia-smi`. На AMD-хостах ставить ROCm/HIP, перевіряє `/dev/kfd`, `/dev/dri` і `rocminfo`, застосовує підписаний AMD Compose overlay. GPU-специфічні overlays лишаються активними під час оновлень, rollback, watchdog-рестартів і startup recovery.

Production runtime має окремі License Manager, Dispatcher, Scheduler і Certificate Manager сервіси. Core чекає здоровими всі вибрані сервіси. Конфігурація Proxy, Prometheus, Loki, Promtail і Grafana вбудована в digest-pinned образи, а не змонтована з мутабельних runtime-файлів.

Deployment Wizard бере список ролей з `config/node_roles.json`; додавання ролі не вимагає зміни token або enrollment-логіки. Core-вузли можуть створювати 15-хвилинні одноразові registration tokens з **Workers → Add Worker**. Non-Core вузли самі ініціюють HTTPS enrollment-запит і отримують node-bound JWT, per-node secret, certificate attestation, конфігурацію і capability set. Dispatch — capability-driven, а не role-driven, тож встановлення нового движка вимагає лише заяви нової capability вузлом.

Release candidates мають проходити відтворювані appliance gates; CI завантажує результуючий JSON-доказ:

```bash
python scripts/qualify-release.py --root . --docker --output qualification.json
```

Integration credentials керуються з **System → Protected integrations**. Значення write-only через API і лишаються всередині автентифікованого шифрованого secret-конверта.

Під час First Run вибраний AI backend контактується до завершення setup: Vertep валідує endpoint, HTTPS-політику, credentials та inventory моделей і може дотягти відсутню локальну Ollama-модель. Фінальний Installation Manifest фіксує вибрану роль і модулі, справжні Docker image digests, стан контейнерів і здоров'я модулів.

Рутинний appliance lifecycle — у **System → Zero-Shell lifecycle**: адміністратори можуть створювати або відновлювати шифровані бекапи, встановлювати чи видаляти Ollama-моделі, переглядати або оновлювати TLS-сертифікат без SSH-сесії. Автентифікований ендпоїнт `/api/system/installation-manifest` повертає поточний inventory інсталяції.
