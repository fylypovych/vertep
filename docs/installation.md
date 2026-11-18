# Встановлення

> Статус: чинний | Аудиторія: оператор | Канон: [AGENTS.md](../AGENTS.md) §12, §15.

## Вимоги

Ubuntu Server 24.04 або 26.04 (amd64/arm64), з активним systemd; обмеження драйверів і пакетів — [runtime readiness](runtime-readiness.md). Базові вимоги: RAM ≥ 4 ГБ, disk ≥ 20 ГБ, Docker + Compose, NTP, порт 8443. GPU: NVIDIA/AMD/відсутня (визначається автоматично; NVIDIA — driver + Container Toolkit, AMD — ROCm/HIP).

## Production-встановлення (одна команда)

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

Перший етап bootstrap — вибір продукту (лише під час першої інсталяції): «1) Vertep» або «2) Лабораторія Vertep (LAB, окрема VM)»; питання ставиться в терміналі навіть за `curl … | sudo bash`, без термінала (CI, автоматизація) встановлюється Vertep, явний вибір — `VERTEP_INSTALL_TARGET=vertep|lab`. Якщо на сервері вже встановлено Vertep (`/opt/vertep`), bootstrap без питань оновлює Vertep; якщо LAB (`/opt/vertep-lab`) — оновлює LAB через `bootstrap-lab.sh` тієї самої релізної версії. Vertep і LAB на одному сервері не поєднуються.

Далі для Vertep: preflight-перевірки → директорії `/opt/vertep/{data,config,logs,backups,storage,models,runtime,tls}` → завантаження runtime з GitHub Release (перевірка підпису manifest і SHA-256) → генерація секретів (`openssl rand -hex`, `/opt/vertep/config/`, `0600`) → запуск базових служб без ролі → `docker compose up` → очікування HEALTHY → міграція БД → health check відповідного підписаного runtime. CORE management/control readiness та готовність вибраних ролей мають різний scope; endpoint HTTP 200 сам по собі не доводить готовності всіх ролей. Фінал: `https://<SERVER-IP>:8443` і Installation Manifest. Повторний запуск безпечний: перевикористовує volumes/секрети, замінює лише release-керовані файли.

## Ролі вузлів

Core / GPU / Text / Voice / Publisher / Backup / Monitoring (склад — `nodes-and-capabilities.md`). Базова роль вузла та додаткові ролі Core задаються під час First Run Wizard; bootstrap про них не питає. Для автоматизованого встановлення — `VERTEP_ROLE` і `VERTEP_ADDITIONAL_ROLES`; застосований набір сумісності зберігається в `NODE_ROLE`, `NODE_ADDITIONAL_ROLES` і `config/deployment-plan.json`. Це legacy/runtime конфігурація вузла, не паралельний каталог модулів. Адміністратор встановлює модулі, погоджує їхні права та задає node placement у Web UI → «Модулі»; readiness показується окремо від вибраного placement. Підключення нового non-Core вузла виконується через Core URL, Certificate і одноразовий Registration Token у Workers → Add Worker. Персонаж за замовчуванням (варіант «За замовчуванням» у Web, API без `character_id`, Telegram) задається `DEFAULT_CHARACTER` — id наявного каталогу `characters/<id>/`: для автоматизованого встановлення передайте `DEFAULT_CHARACTER` у bootstrap або впишіть його в `.env`; значення, задане в `.env`, зберігається при оновленнях. Без нього job потребує явного вибору персонажа (див. `characters-and-workflows.md`).

Роль GPU запускає ComfyUI і потребує NVIDIA або AMD GPU. Якщо на Core додаткову роль `gpu` активовано, а GPU під час встановлення чи оновлення не знайдено (наприклад, Hyper-V VM без passthrough), bootstrap вимикає цю роль: прибирає її з `NODE_ADDITIONAL_ROLES` і `config/deployment-plan.json` і продовжує з рештою ролей. Повернути роль можна на хості з GPU. Окремий GPU-вузол (`NODE_ROLE=gpu`) без GPU одразу завершується помилкою.

## First Run Wizard (9 кроків)

Роль вузла (non-Core: Core URL + Certificate + Registration Token) → назва інсталяції → адміністратор → секрети (автогенерація) → обладнання (автодетект) → AI Backend (Ollama / External OpenAI / External API / Skip) → Health Check → Installation Manifest → Готово (Installation ID, Core URL/Certificate/Token).

## Підключення Worker

Core генерує одноразовий Registration Token (`VT-XXXX-…`, TTL 15 хв, **Workers → Add Worker**). Worker вводить його з Core URL і сертифікатом у Wizard, шле `/api/nodes/register` (CSR, capabilities, hardware) і отримує JWT + Worker Secret + конфігурацію.

## Інші режими

Деінсталяція нових runtime з ownership inventory — [vertep remove](removal.md). Дані й спільні залежності за замовчуванням зберігаються; історичні інсталяції без доказів належності автоматично не видаляються.

- **Demo** (розробка): `cp .env.example .env`, унікальні паролі, `docker compose up --build`, `http://localhost:8080`. Детерміноване зображення замість GPU-моделі; результат — справжній MP4. `LOCAL_WORKER_FALLBACK=true` — лише локальний dev.
- Оновлення, backup, моделі, сертифікати після встановлення — виключно через Web UI (Zero Shell).
