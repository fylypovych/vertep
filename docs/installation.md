# Встановлення

> Статус: чинний | Аудиторія: оператор | Канон: `AGENTS.md` §12–§14, §16

## Вимоги

Чиста Ubuntu Server 24.04 (amd64/arm64), RAM ≥ 4 ГБ, disk ≥ 20 ГБ, Docker + Compose, NTP, порт 8443. GPU: NVIDIA/AMD/відсутня (визначається автоматично; NVIDIA — driver + Container Toolkit, AMD — ROCm/HIP).

## Production-встановлення (одна команда)

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

Bootstrap: preflight-перевірки → директорії `/opt/vertep/{data,config,logs,backups,storage,models,runtime,tls}` → завантаження runtime з GitHub Release (перевірка підпису manifest і SHA-256) → генерація секретів (`openssl rand -hex`, `/opt/vertep/config/`, `0600`) → вибір ролі вузла → `docker compose up` → очікування HEALTHY → міграція БД → health check. Фінал: `https://<SERVER-IP>:8443` і Installation Manifest. Повторний запуск безпечний: перевикористовує volumes/секрети, замінює лише release-керовані файли.

## Ролі вузлів

Core / GPU / Text / Voice / Publisher / Backup / Monitoring (склад — `nodes-and-capabilities.md`). Додаткові ролі Core — `NODE_ADDITIONAL_ROLES` у `.env`.

## First Run Wizard (9 кроків)

Роль вузла (non-Core: Core URL + Certificate + Registration Token) → назва інсталяції → адміністратор → секрети (автогенерація) → обладнання (автодетект) → AI Backend (Ollama / External OpenAI / External API / Skip) → Health Check → Installation Manifest → Готово (Installation ID, Core URL/Certificate/Token).

## Підключення Worker

Core генерує одноразовий Registration Token (`VT-XXXX-…`, TTL 15 хв, **Workers → Add Worker**). Worker вводить його з Core URL і сертифікатом у Wizard, шле `/api/nodes/register` (CSR, capabilities, hardware) і отримує JWT + Worker Secret + конфігурацію.

## Інші режими

- **Demo** (розробка): `cp .env.example .env`, унікальні паролі, `docker compose up --build`, `http://localhost:8080`. Детерміноване зображення замість GPU-моделі; результат — справжній MP4. `LOCAL_WORKER_FALLBACK=true` — лише локальний dev.
- **Legacy/source**: `sudo ./install.sh` (CORE / GPU WORKER / обидва) — для розробки; production — тільки Bootstrap-потік.
- Оновлення, backup, моделі, сертифікати після встановлення — виключно через Web UI (Zero Shell).
