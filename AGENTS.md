# Vertep — правила для агентів

## 1. Місія проєкту

Vertep — оркестратор автоматизованої фабрики короткого медіаконтенту з віртуальними персонажами.  
Vertep **не генерує контент** — він керує інструментами, які генерують контент.  
Це мозок, диспетчер і оркестратор системи. Інструменти: Ollama, ComfyUI, FFmpeg, TTS, Publisher-модулі.

Ключовий принцип:  
> Вертеп — це конвеєр. Моделі змінюються. GPU змінюються. Workflow змінюються. Персонажі додаються й закриваються. А Vertep залишається центральною системою.

---

## 2. Мова

- Користувацькі описи, коміти, нотатки релізів, UI — **українською**.
- Технічні терміни — англійською: API, Docker, PostgreSQL, Redis, ComfyUI, FFmpeg, Ollama, job, scene, task, workflow, worker, CORE, GPU Node, Text Node, Voice Node, Publisher Node, Backup Node, Monitoring Node.

---

## 3. Версіонування та релізи

### 3.1 Нумерація
- Формат: `0.0.0.X` — послідовна.
- Перед вибором номера перевірити наявність тегу в репозиторії: `git tag --list | Select-String "0\.0\.0\."`.
- Якщо тег уже існує — не перезаписувати історію, а вибрати наступний вільний номер.

### 3.2 Один реліз — один коміт
- Заборонено створювати окремий технічний коміт з кодом, а потім коміт від release-бота.
- Той самий коміт містить: усі зміни коду, новий `VERSION`, `CHANGELOG.md`, `releases/<версія>.md`, оновлені тести й документацію.

### 3.3 Назва коміту
```
<версія>
```
Приклад: `0.0.0.89`

### 3.4 Перед створенням релізу
- Додати українські пункти змін до секції Unreleased у `CHANGELOG.md`.
- Виконати повний набір тестів: `python -m pytest -q`.
- Перевірити `git diff --check`.
- Переконатися, що в змінах немає секретів.

### 3.5 Створення релізу
- Всі зміни мають бути закомічені в один релізний коміт з назвою `<версія>`.
- Після коміту запустити: `python scripts/release.py --title "<версія>: <опис українською>"`
- `release.py` сам створить/перемістить тег, запушить коміт, створить GitHub Release і тригне workflow.

### 3.6 Після створення
```bash
python scripts/release.py --check
```

### 3.7 Runtime bundle
- Кожен реліз **обов'язково** має містити артефакт `vertep-runtime-<версія>.tar.gz`.
- GitHub Actions workflow збирає runtime bundle, прикріпляє до release і публікує версійний тег.
- Якщо workflow не створила артефакт — реліз вважається невалідним, bootstrap не зможе встановити систему.
- Не публікувати release, якщо артефакт `vertep-runtime-<версія>.tar.gz` відсутній.
- **Обов'язково**: у GitHub Repository Secrets має бути `RUNTIME_SIGNING_PRIVATE_KEY` для підпису runtime bundle.

### 3.8 Перед push
- Назва коміту відповідає формату.
- `VERSION` містить ту ж версію.
- `CHANGELOG.md` має секцію з цією версією.
- `releases/<версія>.md` існує.
- `git status` чистий.

### 3.9 Push
- `release.py` робить push автоматично.
- Заборонено ручний push до `main` після створення релізу.

### 3.10 GitHub Actions
- Перевіряє релізний коміт.
- **Обов'язково** збирає runtime bundle `vertep-runtime-<версія>.tar.gz` з усіма файлами:
  - `docker-compose.yml`, `docker-compose.amd.yml`, `docker-compose.nvidia.yml`
  - `manifest.json` з цифровим підписом
  - `node_roles.json`, `deployment-plan.py`, `update-agent.py`, `vertep` CLI
  - SBOM файл
  - systemd unit файли
  - моніторинг конфіги
- Створює signed manifest з SHA256 для кожного файлу.
- Прикріпляє runtime bundle до GitHub Release.
- Пубулікує версійний тег і `latest`.
- **Заборонено** для бота: змінювати `VERSION`/`CHANGELOG.md`, створювати додатковий коміт, push у `main`, генерувати англомовний опис.

### 3.11 Після workflow
- Переконатися, що в release є артефакт `vertep-runtime-<версія>.tar.gz`.
- SHA `origin/main` і SHA тега релізу мають збігатися.
- Після релізного коміту не з'являється новий коміт від бота.

### 3.12 Docker-образи
- Кожен реліз збирає всі образи з того самого SHA.
- Тег кожного образу точно збігається з `VERSION`.
- Заборонено: використовувати образи від іншої версії, вручню перезаписувати тег, публікувати якщо хоча б один образ не зібрався, посилатися лише на `latest`.
- Workflow збирає образи, публікує версійний тег і `latest`, записує digest у підписаний маніфест.
- Встановлення використовує immutable digest, не `latest`.

### 3.13 Перевірка перед релізом
- `git tag --list | Select-String "0\.0\.0\."` — переконатися, що тег ще не існує.
- `git ls-remote origin --tags | Select-String "<версія>"` — переконатися, що тег не існує на remote.
- Якщо тег вже існує — не перезаписувати історію, а вибрати наступний вільний номер.

---

## 4. Архітектурні принципи

### 4.1 CORE не генерує контент
- Всі генерації (текст, зображення, відео, голос) виконуються worker-ами через адаптери.
- CORE лише диспетчеризує задачі (`task`) і збирає артефакти.

### 4.2 Персонажі — конфіги, не код
- Нові персонажі додаються через `/characters/<id>/` директорію.
- Кожен має: `character.json`, `system_prompt.txt`, `voice.json`, `visual.json`, `generation.json`, `publishing.json`.
- Не додавати персонажів у хардкод.

### 4.3 Workflow — окремо від коду
- ComfyUI workflow зберігаються в `/workflows/`.
- CORE лише передає шлях workflow в задачі.
- Зміна моделі/LoRA/sampler не потребує зміни коду CORE.

### 4.4 Publisher — модульний
- Кожна платформа (YouTube, TikTok, Facebook, Instagram, Threads) — окремий адаптер.
- CORE передає готовий ролик і метадані, Publisher виконує публікацію.

### 4.5 Web UI
- Локалізація: українська (`admin-uk.js`, `admin-uk.css`).
- Дії: Pause / Resume / Retry / Regenerate / Cancel / Delete / Approve / Publish.

### 4.6 Інсталяція — Deployment Wizard
- Один bootstrap.sh на чисту Ubuntu 24.04.
- Користувач вибирає роль вузла: Core / GPU / Text / Voice / Publisher / Backup / Monitoring.
- Кожна роль встановлює лише необхідні компоненти.
- Після bootstrap — First Run Wizard через Web UI.
- Після завершення — користувач працює виключно через Web UI (Zero Shell).
- Всі сервіси — у Docker контейнерах, volumes для даних.
- Секрети генеруються автоматично, зберігаються у внутрішньому сховищі.

### 4.7 Immutable Runtime
- Користувач не змінює контейнер вручну.
- Будь-які зміни — через Web UI або офіційні оновлення.
- Якщо контейнер пошкоджено — замінюються новим образом.
- Усі дані, конфігурація, БД — у volumes.

---

## 5. Життєвий цикл Job

```
NEW → SCRIPTING → SCRIPT_READY → ASSET_GENERATION → ASSETS_READY → 
VIDEO_GENERATION → VIDEO_READY → ASSEMBLY → READY → PUBLISHING → PUBLISHED
```

Бокові стани: `PAUSED`, `FAILED`, `CANCELLED`, `WAITING_FOR_SYSTEM`.

---

## 6. Джерела контенту

Перше джерело — Telegram. Подальші (RSS, сайти, API, ручне створення, тренди) додавати як модулі, не змінюючи ядро.

---

## 7. Ролі вузлів

- `core` — CORE + API + Dispatcher + Scheduler + Publisher + Redis + PostgreSQL + License Manager + Update Agent + Monitoring.
- `gpu` — Worker + ComfyUI + CUDA Runtime + Update Agent.
- `text` — Worker + Ollama + LLM Runtime + Update Agent.
- `voice` — Worker + TTS Runtime + Voice Models + Update Agent.
- `publisher` — Publisher Worker + платформи + Update Agent.
- `backup` — Backup Service + Snapshot Manager + Archive Service + Update Agent.
- `monitoring` — Grafana + Prometheus + Logs + Metrics + Update Agent.

Додаткові ролі активуються через `NODE_ADDITIONAL_ROLES` у `.env`.

---

## 8. Capability-driven архітектура

- Роль визначає, що встановити.
- Capabilities визначають, що вузол реально може виконувати.
- Dispatcher шукає вузол за потрібною capability, а не за роллю.
- Приклад capabilities: `image_generation`, `image_upscale`, `controlnet`, `inpainting`, `video_generation`, `tts`, `publish_youtube`, `publish_tiktok`.
- Worker сам повідомляє Core свої capabilities при реєстрації.

---

## 9. Відмовостійкість

- `recover_after_restart()` відновлює Job після перезавантаження CORE.
- Worker надсилають heartbeat.
- Після reboot основні компоненти запускаються автоматично через systemd.
- Startup recovery service відновлює стан системи.

---

## 10. Глобальні стани системи

```
NORMAL — штатна робота.
MAINTENANCE — нові задачі лише накопичуються.
UPDATING — жодні задачі не запускаються.
RECOVERING — відновлення після невдалого оновлення.
READ_ONLY — тільки читання.
EMERGENCY — лише аварійні дії адміністратора.
```

Усі модулі орієнтуються на глобальний стан через `get_system_state()`.

---

## 11. Safe Update System

### 11.1 Джерело оновлень
- Оновлення отримуються виключно з `https://update.vertep.ai` (або вказаного `VERTEP_UPDATE_SERVER`).
- GitHub використовується лише командою розробки.

### 11.2 Запуск оновлення
- Автоматично: періодична перевірка (раз на 6 годин через systemd timer).
- Через Web UI: Settings → Update → Install.
- Через CLI: `vertep update`.

### 11.3 Перехід у Maintenance
- Перед оновленням система переходить у `MAINTENANCE`.
- Dispatcher перестає видавати нові задачі.
- Активні задачі продовжують виконуватися.
- Нові задачі отримують `WAITING_FOR_SYSTEM`.

### 11.4 Drain Mode
- Dispatcher переходить у `DRAIN`.
- Нові задачі не запускаються.
- Worker завершують поточну роботу і повідомляють Core: `FREE`.

### 11.5 Backup перед оновленням
- Автоматично створюється резервна копія: конфігурація, PostgreSQL, Redis, локальні налаштування, ліцензії, секрети, журнали міграцій.
- Зберігається у `/opt/vertep/backups/update-<stamp>/`.

### 11.6 Оновлення
- Завантажуються нові контейнери.
- Виконується міграція БД.
- Перезапускаються сервіси.

### 11.7 Health Check після оновлення
- Перевірка: PostgreSQL, Redis, API, Web UI, Ollama, Dispatcher, GPU, Driver, CUDA, ComfyUI, VRAM.
- Кожен Worker проходить самотестування (мінімальний workflow).
- Лише після успішного завершення: `STATUS = READY`.

### 11.8 Rollback
- Якщо будь-який Health Check завершився помилкою — оновлення скасовується.
- Автоматично виконується: відновлення попередньої версії, контейнерів, БД, запуск попередньої конфігурації.
- `vertep rollback` — ручний rollback через CLI.

### 11.9 Rolling Update
- Оновлює вузли по одному (one-node-at-a-time).
- Canary promotion: перший вузол оновлюється, проходить self-test, потім підтверджується (promote) або відкочується (rollback).
- Підтримка порядків: `workers-first`, `core-first`, `custom`.
- Автоматичний rollback при таймауті або помилці.

### 11.10 Логування
- Окремий журнал оновлень: `/data/config/update/status.json`, `/data/config/update/log/`.
- Кожен запис: час, фаза, статус, повідомлення.

---

## 12. Bootstrap Installer

### 12.1 Єдина команда
```bash
curl -fsSL https://download.vertep.ai/bootstrap.sh | sudo bash
```
або
```bash
wget https://download.vertep.ai/bootstrap.sh
sudo bash bootstrap.sh
```

### 12.2 Перевірки
- Ubuntu 24.04 LTS, amd64/arm64.
- RAM ≥ 4096 MB, disk ≥ 20480 MB.
- GPU: NVIDIA/AMD/відсутня.
- Docker, Docker Compose.
- Сетьова доступність, NTP, порт 8443.

### 12.3 Створення директорій
```
/opt/vertep/{data,config,logs,backups,storage,models,runtime,tls}
```

### 12.4 Завантаження runtime
- Отримує `docker-compose.yml`, образи, стартові конфігурації з сервера Vertep.
- Перевіряє цифровий підпис manifest.
- Перевіряє SHA256 кожного файлу.

### 12.5 Генерація секретів
- PostgreSQL Password, Redis Password, JWT Secret, Worker Secret, Encryption Key, Internal API Key, Session Secret, SSL Certificates.
- Криптографічно стійкий генератор (`openssl rand -hex`).
- Зберігаються у `/opt/vertep/config/` з permissions `0600`.

### 12.6 Вибір ролі
- Користувач вибирає роль вузла: Core / GPU / Text / Voice / Publisher / Backup / Monitoring.
- Додаткові ролі для Core: gpu, text, voice, publisher, backup, monitoring.
- Ролі зберігаються у `deployment-plan.json`.

### 12.7 Запуск контейнерів
- `docker compose up -d --remove-orphans`.
- Очікування HEALTHY для всіх сервісів.
- Міграція БД.
- Health check через `/api/health`.

### 12.8 Завершення
- Відображається `https://<SERVER-IP>:8443` або `https://<setup_url>/setup?token=<TOKEN>`.
- Відкривається браузер (якщо можливо).
- Генерується Installation Manifest.

---

## 13. First Run Wizard

### 13.1 Кроки
1. **Роль вузла** — вибір зі списку. Для non-Core: введення Core URL, Core Certificate, Registration Token.
2. **Назва інсталяції** — ім'я системи, домен (необов'язково).
3. **Адміністратор** — login, password, підтвердження.
4. **Секрети** — автоматично згенеровані, користувач бачить лише повідомлення.
5. **Обладнання** — автовизначення GPU, RAM, CPU.
6. **AI Backend** — Ollama / External OpenAI / External API / Skip.
7. **Health Check** — автоматична перевірка всіх сервісів.
8. **Installation Manifest** — перегляд, можливість завантажити JSON.
9. **Готово** — Installation ID, Core URL, Core Certificate, Registration Token.

### 13.2 Core Wizard
- Генерує перший Registration Token для підключення Worker-ів.
- Відображає Core Certificate для імпорту на Worker-и.

### 13.3 Non-Core Wizard
- Вводить Core URL, Core Certificate, Registration Token.
- Отримує JWT, Worker Secret, Configuration.
- Реєструється у Core через `/api/nodes/register`.

---

## 14. Registration Token

- Генерується Core для підключення Worker-ів.
- Формат: `VT-XXXX-XXXX-XXXX` (32 hex символи).
- TTL: 15 хвилин (за замовчуванням).
- Зберігається в базі даних (`node_registration_tokens`).
- Використовується один раз, потім видаляється.

---

## 15. Node Registry

- Core веде реєстр усіх вузлів.
- Кожен вузол має: `node_id`, `role`, `capabilities`, `hardware`, `version`, `status`, `last_heartbeat`.
- Worker надсилає heartbeat кожні 30 секунд.
- Core автоматично визначає `ONLINE / BUSY / FREE / UPDATING / OFFLINE / ERROR`.

---

## 16. Worker Registration Flow

1. Bootstrap встановлює роль, генерує CSR.
2. Користувач вводить Core URL + Registration Token у Wizard.
3. Worker відправляє `/api/nodes/register` з CSR, capabilities, hardware.
4. Core перевіряє токен, видає JWT + Worker Secret + Configuration.
5. Worker зберігає credentials, запускає сервіси.
6. Worker з'являється у Web UI Core.

---

## 17. GPU Dispatcher

- CORE постійно знає стан усіх WORKER.
- При створенні GPU-задачі вказуються вимоги: `IMAGE / MIN_VRAM 6 GB` або `VIDEO / MIN_VRAM 20 GB`.
- Dispatcher сам визначає, на яку машину відправити задачу.
- Підтримує різні GPU: NVIDIA, AMD.
- Worker сам повідомляє свої capabilities при реєстрації.

---

## 18. Сценарний агент

- Генерація сценарію — багатоступінчаста:
  1. **Structure**: заголовок, опис, хештеги, план сцен.
  2. **Scenes**: для кожної сцени окремий LLM-виклик з промптом персонажа + сценарію.
  3. **Assembly**: збір фінального script, нормалізація через `ScriptDocument`.
- Реалізація: `core/script_agent.py`.
- Виклик: `ScriptAgent().generate_script()` в `core/pipeline.py:prepare_job()`.
- Fallback: якщо сцена невалідна — перегенерувати лише її, не весь сценарій.
- Персонаж передається як конфіг, не хардкод.

---

## 19. ComfyUI Executor

- Додавати в `worker/role_executor.py` як `execute_image()`/`execute_video()`.
- Реєструвати в `EXECUTORS`/`ROLE_TASKS`.
- Не викликати `ComfyUIAdapter` напряму в `worker/service.py` — використовувати `execute_role_task()`.
- Fallback для відео: якщо ComfyUI не повернув відео — зібрати з кадрів через FFmpeg.

---

## 20. FFmpeg пайплайн

- Збірка виконується через `FFmpegAdapter` в `core/pipeline.py:finalize_job()`.
- Не переносити на worker без явного завдання.
- Підтримує: відеоряд, pan/zoom, переходи, голос, музика, субтитри, заставки, фінальне кодування.
- Принцип: не витрачати GPU на те, що нормально робиться звичайним монтажем.

---

## 21. Тестування

- Перед кожним релізом: `python -m pytest -q`.
- Допустимі невдачі: тільки e2e через `ERR_CONNECTION_REFUSED 127.0.0.1:8080` (браузерні тести без запущеного сервера).
- Будь-які нові падіння в `tests/test_features.py` або інших модульних тестах — блокують реліз.
- Інтеграційні тести для кожного executor (ComfyUI, FFmpeg, TTS, Publisher).

---

## 22. Безпека

- Не комітити секрети.
- Використовувати `integration_secret` для збереження токенів.
- mTLS для worker-ів за потреби (`NODE_MTLS_REQUIRED=true`).
- Setup-токен з лімітом спроб і терміном дії.
- Усі секрети зберігаються у `/opt/vertep/config/` з permissions `0600`.
- TLS сертифікати: веб (`vertep.crt`/`vertep.key`) + node CA (`node-ca.crt`/`node-ca.key`).

---

## 23. Docker та Volumes

- Усі сервіси працюють виключно в контейнерах.
- Дані контейнерів зберігаються у Docker Volumes:
  - `vertep_postgres-data`
  - `vertep_redis-data`
  - `vertep_storage`
  - `vertep_logs`
  - `vertep_models`
  - `vertep_configs`
- Користувач не має доступу до контейнерів безпосередньо.

---

## 24. Масштабування

- Додавання нової ролі — через bootstrap + wizard, не через зміну коду.
- Додавання нової capability — через конфіг worker-а, не через зміну коду CORE.
- Нові персонажі — через `/characters/<id>/` директорію, не через код.
- Нові Publisher-платформи — через новий адаптер у `publishers/<platform>/`.

---

## 25. Заборонено

- Змінювати `VERSION` або `CHANGELOG.md` в окремому коміті перед релізом.
- Створювати додатковий коміт після релізного.
- Використовувати `latest` тег для production-інсталяцій.
- Пуш в `main` без попереднього запуску тестів.
- Комітити секрети, паролі, ключі.
- Хардкодити персонажів, workflow, secrets.
- Викликати `ComfyUIAdapter` напряму в `worker/service.py` — використовувати `execute_role_task()`.
- Блокувати event loop CORE блокуючими викликами FFmpeg — використовувати `ThreadPoolExecutor`.

---

## 26. Принцип "Infrastructure as Product"

- Vertep сприймається як єдиний програмний продукт, не як набір окремих компонентів.
- Користувач взаємодіє лише з брендом Vertep.
- Внутрішні технології (Docker, PostgreSQL, Redis, Ollama, CUDA) — деталі реалізації.
- Весь адміністрування через Web UI, не через shell.
