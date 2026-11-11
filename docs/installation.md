# Встановлення

> Статус: чинний | Аудиторія: оператор | Канон: [AGENTS.md](../AGENTS.md) §12, §15.

## Вимоги

Ubuntu Server 24.04 або 26.04 (amd64/arm64), з активним systemd; обмеження драйверів і пакетів — [runtime readiness](runtime-readiness.md). Базові вимоги: RAM ≥ 4 ГБ, disk ≥ 20 ГБ, Docker + Compose, NTP, порт 8443. GPU: NVIDIA/AMD/відсутня (визначається автоматично; NVIDIA — driver + Container Toolkit, AMD — ROCm/HIP).

## Production-встановлення (одна команда)

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

Bootstrap: preflight-перевірки → директорії `/opt/vertep/{data,config,logs,backups,storage,models,runtime,tls}` → завантаження runtime з GitHub Release (перевірка підпису manifest і SHA-256) → генерація секретів (`openssl rand -hex`, `/opt/vertep/config/`, `0600`) → запуск базових служб без ролі → `docker compose up` → очікування HEALTHY → міграція БД → health check відповідного підписаного runtime. CORE management/control readiness та готовність вибраних ролей мають різний scope; endpoint HTTP 200 сам по собі не доводить готовності всіх ролей. Фінал: `https://<SERVER-IP>:8443` і Installation Manifest. Повторний запуск безпечний: перевикористовує volumes/секрети, замінює лише release-керовані файли.

## Ролі вузлів

Core / GPU / Text / Voice / Publisher / Backup / Monitoring (склад — `nodes-and-capabilities.md`). Роль і додаткові ролі Core обираються в First Run Wizard; bootstrap про них не питає. Для автоматизованого встановлення — `VERTEP_ROLE` і `VERTEP_ADDITIONAL_ROLES`; результат зберігається в `NODE_ROLE` і `NODE_ADDITIONAL_ROLES` у `.env`.

## First Run Wizard (9 кроків)

Роль вузла (non-Core: Core URL + Certificate + Registration Token) → назва інсталяції → адміністратор → секрети (автогенерація) → обладнання (автодетект) → AI Backend (Ollama / External OpenAI / External API / Skip) → Health Check → Installation Manifest → Готово (Installation ID, Core URL/Certificate/Token).

## Підключення Worker

Core генерує одноразовий Registration Token (`VT-XXXX-…`, TTL 15 хв, **Workers → Add Worker**). Worker вводить його з Core URL і сертифікатом у Wizard, шле `/api/nodes/register` (CSR, capabilities, hardware) і отримує JWT + Worker Secret + конфігурацію.

## Інші режими

Деінсталяція нових runtime з ownership inventory — [vertep remove](removal.md). Дані й спільні залежності за замовчуванням зберігаються; історичні інсталяції без доказів належності автоматично не видаляються.

- **Demo** (розробка): `cp .env.example .env`, унікальні паролі, `docker compose up --build`, `http://localhost:8080`. Детерміноване зображення замість GPU-моделі; результат — справжній MP4. `LOCAL_WORKER_FALLBACK=true` — лише локальний dev.
- **Legacy/source**: `sudo ./install.sh` (CORE / GPU WORKER / обидва) — для розробки; production — тільки Bootstrap-потік.
- Оновлення, backup, моделі, сертифікати після встановлення — виключно через Web UI (Zero Shell).
