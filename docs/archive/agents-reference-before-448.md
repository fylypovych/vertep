# Довідкові розділи AGENTS.md до #448

> Статус: історичний знімок 2026-11-11 | Аудиторія: власник, розробник | Канон: [чинний AGENTS.md](../../AGENTS.md).

Збережений текст §5–§15 перед погодженою реорганізацією #448. Застарілі виклики, сховища, нумерація й приклади нижче не є поточним описом реалізації. Нормативні обмеження збережено в чинному AGENTS.md; операторські процедури зведено з тематичними настановами. Цей знімок не дозволяє виконувати наведені команди.

## 5. Місія проєкту

Vertep — оркестратор автоматизованої фабрики короткого медіаконтенту з віртуальними персонажами.  
Vertep **не генерує контент** — він керує інструментами, які генерують контент.  
Це мозок, диспетчер і оркестратор системи. Інструменти: Ollama, ComfyUI, FFmpeg, TTS, Publisher-модулі.

Ключовий принцип:  
> Вертеп — це конвеєр. Моделі змінюються. GPU змінюються. Workflow змінюються. Персонажі додаються й закриваються. А Vertep залишається центральною системою.

---

## 6. Життєвий цикл Job

```
NEW → SCRIPTING → SCRIPT_READY → ASSET_GENERATION → ASSETS_READY → 
VIDEO_GENERATION → VIDEO_READY → ASSEMBLY → READY → PUBLISHING → PUBLISHED
```

Бокові стани: `PAUSED`, `FAILED`, `CANCELLED`, `WAITING_FOR_SYSTEM`.

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

## 11. Тестування

- Перед кожним релізом: `python -m pytest -q`.
- Допустимі невдачі: тільки e2e через `ERR_CONNECTION_REFUSED 127.0.0.1:8080` (браузерні тести без запущеного сервера).
- Будь-які нові падіння в `tests/test_features.py` або інших модульних тестах — блокують реліз.
- Інтеграційні тести для кожного executor (ComfyUI, FFmpeg, TTS, Publisher).
- Release workflow зобов'язаний перевіряти Browser E2E gate для точного SHA (§2.12); green Release при failed Browser gate — порушення.

---

## 12. Безпека

- Не комітити секрети.
- Використовувати `integration_secret` для збереження токенів.
- mTLS для worker-ів за потреби (`NODE_MTLS_REQUIRED=true`).
- Setup-токен з лімітом спроб і терміном дії.
- Секрети інтеграцій і внутрішні ключі зберігаються в PostgreSQL у зашифрованому вигляді. Вузли отримують секрет лише через CORE і лише для каналу поточної задачі.
- Пароль БД, ключ шифрування сховища та інші секрети запуску зберігаються у `/opt/vertep/config/` з permissions `0600`.
- TLS сертифікати: веб (`vertep.crt`/`vertep.key`) + node CA (`node-ca.crt`/`node-ca.key`).

---

## 13. Принцип "Infrastructure as Product"

- Vertep сприймається як єдиний програмний продукт, не як набір окремих компонентів.
- Користувач взаємодіє лише з брендом Vertep.
- Внутрішні технології (Docker, PostgreSQL, Redis, Ollama, CUDA) — деталі реалізації.
- Весь адміністрування через Web UI, не через shell.

---

## 14. Технічні деталі

### 14.1 Джерела контенту

Перше джерело — Telegram. Подальші (RSS, сайти, API, ручне створення, тренди) додавати як модулі, не змінюючи ядро.

### 14.2 Сценарний агент

- Генерація сценарію — багатоступінчаста:
  1. **Structure**: заголовок, опис, хештеги, план сцен.
  2. **Scenes**: для кожної сцени окремий LLM-виклик з промптом персонажа + сценарію.
  3. **Assembly**: збір фінального script, нормалізація через `ScriptDocument`.
- Реалізація: `core/script_agent.py`.
- Виклик: `ScriptAgent().generate_script()` в `core/pipeline.py:prepare_job()`.
- Fallback: якщо сцена невалідна — перегенерувати лише її, не весь сценарій.
- Персонаж передається як конфіг, не хардкод.

### 14.3 ComfyUI Executor

- Додавати в `worker/role_executor.py` як `execute_image()`/`execute_video()`.
- Реєструвати в `EXECUTORS`/`ROLE_TASKS`.
- Не викликати `ComfyUIAdapter` напряму в `worker/service.py` — використовувати `execute_role_task()`.
- Fallback для відео: якщо ComfyUI не повернув відео — зібрати з кадрів через FFmpeg.

### 14.4 FFmpeg пайплайн

- Збірка виконується через `FFmpegAdapter` в `core/pipeline.py:finalize_job()`.
- FFmpeg-монтаж у CORE виконується лише через `AssemblyProvider` (§3.5) і належить до control-plane (§3.1).
- Не переносити на worker без явного завдання.
- Підтримує: відеоряд, pan/zoom, переходи, голос, музика, субтитри, заставки, фінальне кодування.
- Принцип: не витрачати GPU на те, що нормально робиться звичайним монтажем.

### 14.5 GPU Dispatcher

- CORE постійно знає стан усіх WORKER.
- При створенні GPU-задачі вказуються вимоги: `IMAGE / MIN_VRAM 6 GB` або `VIDEO / MIN_VRAM 20 GB`.
- Dispatcher сам визначає, на яку машину відправити задачу. Вузол забирає задачу сам; пріоритетів вузлів немає, а між вільними ідентичними вузлами задачі розподіляються по черзі (`core/turns.py`).
- При збої GPU-провайдера автоматичного перемикання немає: оператор бачить повідомлення про потребу перезапуску.
- Підтримує різні GPU: NVIDIA, AMD.
- Worker сам повідомляє свої capabilities при реєстрації.

### 14.6 Docker та Volumes

- Усі сервіси працюють виключно в контейнерах.
- Дані контейнерів зберігаються у Docker Volumes:
  - `vertep_postgres-data`
  - `vertep_redis-data`
  - `vertep_storage`
  - `vertep_logs`
  - `vertep_models`
  - `vertep_configs`
- Користувач не має доступу до контейнерів безпосередньо.

### 14.7 Масштабування

- Додавання нової ролі — через bootstrap + wizard, не через зміну коду.
- Додавання нової capability — через конфіг worker-а, не через зміну коду CORE.
- Нові персонажі — через `/characters/<id>/` директорію, не через код.
- Нові Publisher-платформи — через новий адаптер у `publishers/<platform>/`.

---

## 15. Інсталяція та оновлення

### 15.1 Bootstrap Installer

#### 15.1.1 Єдина команда
```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```
або
```bash
wget https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh
sudo bash bootstrap.sh
```

#### 15.1.2 Перевірки
- Ubuntu 24.04 LTS, amd64/arm64.
- RAM ≥ 4096 MB, disk ≥ 20480 MB.
- GPU: NVIDIA/AMD/відсутня.
- Docker, Docker Compose.
- Сетьова доступність, NTP, порт 8443.

#### 15.1.3 Створення директорій
```
/opt/vertep/{data,config,logs,backups,storage,models,runtime,tls}
```

#### 15.1.4 Завантаження runtime
- Отримує `docker-compose.yml`, стартові конфігурації та посилання на образи з GitHub Release; образи — з GHCR цього репозиторію за digest.
- Перевіряє цифровий підпис manifest.
- Перевіряє SHA256 кожного файлу.

#### 15.1.5 Генерація секретів
- PostgreSQL Password, Redis Password, JWT Secret, Worker Secret, Encryption Key, Internal API Key, Session Secret, SSL Certificates.
- Криптографічно стійкий генератор (`openssl rand -hex`).
- Зберігаються у `/opt/vertep/config/` з permissions `0600`.

#### 15.1.6 Вибір ролі
- Bootstrap не питає роль: користувач вибирає роль вузла в First Run Wizard (§15.2): Core / GPU / Text / Voice / Publisher / Backup / Monitoring. Для автоматизації — `VERTEP_ROLE` / `VERTEP_ADDITIONAL_ROLES`.
- Додаткові ролі для Core: gpu, text, voice, publisher, backup, monitoring.
- Ролі зберігаються у `deployment-plan.json`.

#### 15.1.7 Запуск контейнерів
- `docker compose up -d --remove-orphans`.
- Очікування HEALTHY для всіх сервісів.
- Міграція БД.
- Health check через `/api/health`.

#### 15.1.8 Завершення
- Відображається `https://<SERVER-IP>:8443` або `https://<setup_url>/setup?token=<TOKEN>`.
- Відкривається браузер (якщо можливо).
- Генерується Installation Manifest.

### 15.2 First Run Wizard

#### 15.2.1 Кроки
1. **Роль вузла** — вибір зі списку. Для non-Core: введення Core URL, Core Certificate, Registration Token.
2. **Назва інсталяції** — ім'я системи, домен (необов'язково).
3. **Адміністратор** — login, password, підтвердження.
4. **Секрети** — автоматично згенеровані, користувач бачить лише повідомлення.
5. **Обладнання** — автовизначення GPU, RAM, CPU.
6. **AI Backend** — Ollama / External OpenAI / External API / Skip.
7. **Health Check** — автоматична перевірка всіх сервісів.
8. **Installation Manifest** — перегляд, можливість завантажити JSON.
9. **Готово** — Installation ID, Core URL, Core Certificate, Registration Token.

#### 15.2.2 Core Wizard
- Генерує перший Registration Token для підключення Worker-ів.
- Відображає Core Certificate для імпорту на Worker-и.

#### 15.2.3 Non-Core Wizard
- Вводить Core URL, Core Certificate, Registration Token.
- Отримує JWT, Worker Secret, Configuration.
- Реєструється у Core через `/api/nodes/register`.

### 15.3 Registration Token

- Генерується Core для підключення Worker-ів.
- Формат: `VT-XXXX-XXXX-XXXX` (32 hex символи).
- TTL: 15 хвилин (за замовчуванням).
- Зберігається в базі даних (`node_registration_tokens`).
- Використовується один раз, потім видаляється.

### 15.4 Node Registry

- Core веде реєстр усіх вузлів.
- Кожен вузол має: `node_id`, `role`, `capabilities`, `hardware`, `version`, `status`, `last_heartbeat`.
- Worker надсилає heartbeat кожні 30 секунд.
- Core автоматично визначає `ONLINE / BUSY / FREE / UPDATING / OFFLINE / ERROR`.

### 15.5 Worker Registration Flow

1. Bootstrap встановлює роль, генерує CSR.
2. Користувач вводить Core URL + Registration Token у Wizard.
3. Worker відправляє `/api/nodes/register` з CSR, capabilities, hardware.
4. Core перевіряє токен, видає JWT + Worker Secret + Configuration.
5. Worker зберігає credentials, запускає сервіси.
6. Worker з'являється у Web UI Core.

### 15.6 Safe Update System

#### 15.6.1 Джерело оновлень
- Канонічне джерело коду, релізів і runtime-артефактів — GitHub-репозиторій `https://github.com/fylypovych/vertep`: production-інсталяції споживають лише підписані GitHub Releases.
- Оновлення отримуються з GitHub Releases (або вказаного `VERTEP_UPDATE_SERVER`, який має віддавати ті самі підписані артефакти).
- Збірки з гілок/комітів поза Release не є джерелом оновлень production-інсталяцій.

#### 15.6.2 Запуск оновлення
- Автоматично: періодична перевірка (раз на 6 годин через systemd timer).
- Через Web UI: Settings → Update → Install.
- Через CLI: `vertep update`.

#### 15.6.3 Перехід у Maintenance
- Перед оновленням система переходить у `MAINTENANCE`.
- Dispatcher перестає видавати нові задачі.
- Активні задачі продовжують виконуватися.
- Нові задачі отримують `WAITING_FOR_SYSTEM`.

#### 15.6.4 Drain Mode
- Dispatcher переходить у `DRAIN`.
- Нові задачі не запускаються.
- Worker завершують поточну роботу і повідомляють Core: `FREE`.

#### 15.6.5 Backup перед оновленням
- Автоматично створюється резервна копія: конфігурація, PostgreSQL, Redis, локальні налаштування, ліцензії, секрети, журнали міграцій.
- Зберігається у `/opt/vertep/backups/update-<stamp>/`.

#### 15.6.6 Оновлення
- Завантажуються нові контейнери.
- Виконується міграція БД.
- Перезапускаються сервіси.

#### 15.6.7 Health Check після оновлення
- Перевірка: PostgreSQL, Redis, API, Web UI, Ollama, Dispatcher, GPU, Driver, CUDA, ComfyUI, VRAM.
- Кожен Worker проходить самотестування (мінімальний workflow).
- Лише після успішного завершення: `STATUS = READY`.

#### 15.6.8 Rollback
- Якщо будь-який Health Check завершився помилкою — оновлення скасовується.
- Автоматично виконується: відновлення попередньої версії, контейнерів, БД, запуск попередньої конфігурації.
- `vertep rollback` — ручний rollback через CLI.

#### 15.6.9 Rolling Update
- Оновлює вузли по одному (one-node-at-a-time).
- Canary promotion: перший вузол оновлюється, проходить self-test, потім підтверджується (promote) або відкочується (rollback).
- Підтримка порядків: `workers-first`, `core-first`, `custom`.
- Автоматичний rollback при таймауті або помилці.

#### 15.6.10 Логування
- Окремий журнал оновлень: `/data/config/update/status.json`, `/data/config/update/log/`.
- Кожен запис: час, фаза, статус, повідомлення.

---

