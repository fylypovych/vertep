# Vertep — карта лабораторії розробки (Issue #107 / var.0.0.1.7)

> Статус: впроваджується | Аудиторія: власник, розробник | Канон: `AGENTS.md` §4.6–§4.7

Окрема, економна автономна лабораторія розробки для репозиторію Vertep на
Ubuntu VM: регулярний аудит коду, локальні виправлення в ізольованій копії та
повторна перевірка без платних підписок і без втручання в робочі Vertep.

## Що це не є

* Лабораторія **не** публікує код. `push`, `release`, гілки, PR і будь-які
  зміни `i`/`ir`/`var` доступні лише через власника (`AGENTS.md` §0.1–§0.2).
* Це не встановлюваний модуль Vertep і не змінює production runtime.
  Архітектурний перехід опишеться в `var.0.0.1.8`.
* Реальні перевірки на окремій VM і фізичному GPU залишаються `ir`
  (критерії приймання 4–6); це файл містить лише автоматизовану реалізацію.

## Структура

* `lab/` — ізольована підсистема:
  * `policy.py` — fail-closed policy gate, STANDING_PERMISSIONS, GATED_ACTIONS, FORBIDDEN_ACTIONS
  * `audit.py` — hash-chained audit trail, tamper-evident
  * `gpu_lease.py` — GPU idle-lease контракт з production dispatcher
  * `budget.py` — бюджет/квоти, rate limit handling
  * `orchestrator.py` — гібридний оркестратор (software + AI half)
  * `governance.py` — GitHub governance (лише `lab` issues)
  * `workspace.py` — ізоляція робочої копії
  * `state_store.py` — персистентність стану
  * `cli.py` — CLI клієнт
  * `api.py` — FastAPI backend для Web панелі
  * `role_profiles.md` — профілі 8 агентних ролей
* `deploy/lab/` — Docker Compose для лабораторії на окремій VM, Dockerfile.
* `installer/vertep-lab.service` + `.timer` — системні одиниці для планувального
  запуску циклу лабораторії.
* `docs/lab.md` — ця документація.
* `tests/test_lab_*.py` — контрактні тести (118 passed).

## Правила лабораторії

1. Лише в ізольованій копії. Жодного write до produkcyjного Docker socket, БД,
   volumes, secrets, конфігурації.
2. Аudit trail кожної дії; bypass через CLI/агент/Web неможливий.
3. GPU тільки за lease від production dispatcher; Vertep має абсолютний
   пріоритет; після рестарту lease відстрочується, безпечне звільнення
   гарантоване, інакше лабораторія не отримує GPU.
4. Безкоштовні моделі лише; платні API відмовляються.
5. Статичні перевірки завжди перед будь-якою AI-компанією.

## Режим використання

```bash
python -m lab.cli status
python -m lab.cli audit
python -m lab.cli check create_lab_issue --subject lab.0.0.0.1
python -m lab.cli grant create_lab_issue --approved-by owner --hours 8
```

Для планового запуску: `installer/vertep-lab.timer` (нічні цикли).

---

## Виконані етапи (2026-10-18)

### Етап 1 — Фундамент ✅
- Всі базові модулі `lab/` реалізовано та протестовано
- `pytest -q tests/test_lab_*.py` — 118 passed
- `python -m compileall -q lab` — без помилок
- Архітектура відповідає `AGENTS.md` §0.1, §4.6, §4.7

### Етап 2 — Агентні ролі та Skills ✅
- Створено `lab/role_profiles.md` з профілями 8 ролей:
  - architect, backend, ai_gpu, frontend, devops, qa, security, reviewer
- Додано `tests/test_lab_role_profiles.py` — 10 тестів
- Ревізор ізольований від авторів змін (read-only)
- MCP не потрібні на цьому етапі

### Етап 3 — Acceptance policy gate ✅
- Усі 7 вимог §4.6/§4.7 покрито тестами
- Додано `tests/test_lab_ir_acceptance.py` — 5 тестів
- Додано `tests/test_lab_acceptance_criteria.py` — 9 тестів
- Автоматичне закриття `ir` лише після реальних доказів
- Атомарна послідовність перевірена
- Audit trail записує всі рішення

---

## Наступні етапи (потребують реальної VM/GPU)

### Етап 4 — GPU idle-lease інтеграція
**Мета:** підключити `LeaseManager` до реального production dispatcher.

**Контракт:**
- `VertepResourceClient` говорить з production endpoint
- Request → grant/deny → lease → heartbeat/renew → revoke → release confirmed
- Status/health — окремі read-only операції

**Перевірки на реальному стенді:**
1. Налаштувати `LAB_VERTEP_URL` та `LAB_VERTEP_TOKEN`
2. Перевірити контракт `/status`, `/lease/request`, `/lease/heartbeat`, `/lease/release`
3. Перевірити витіснення: при production навантаженні lab зупиняє нові requests
4. Перевірити `LeaseLost` при відклику production
5. Перевірити `_restore` після краху/рестарту
6. Перевірити звільнення lease в `finally` навіть при `PolicyDenied`

**Вимоги до production dispatcher:**
- Повинен надавати HTTP endpoint з версійованим контрактом
- Повинен перевіряти `X-Vertep-Lab-Token` header
- Повинен повертати `contract_version` у status
- Повинен відкликати lease при появі production роботи

### Етап 5 — Docker-розгортання та systemd
**Мета:** запустити лабораторію в ізольованому Docker-середовищі.

**Конфігурація вже присутня:**
- `deploy/lab/docker-compose.yml` — два сервіси: lab-control, lab-sandbox
- `deploy/lab/Dockerfile` — мінімальний образ
- `deploy/lab/Dockerfile.sandbox` — ultra-secure sandbox для виконання коду
- `installer/vertep-lab.service` + `.timer` — systemd для планувального запуску

**Перевірки на реальній VM:**
1. `docker compose up` без помилок
2. Мережева ізоляція: доступ лише до дозволених endpoints
3. Ресурси VM: 4–8 vCPU, 16–32 ГБ RAM, 100–200 ГБ SSD
4. GPU НЕ передається через passthrough — inference тільки через мережу
5. `python -m lab.cli status` всередині контейнера

### Етап 6 — Перший повноцінний аудит
**Мета:** запустити реальний аудит коду, тестів, документації.

**Процес:**
1. Оркестратор створює ізольовану Git-копію
2. Статичні перевірки перед AI-викликами
3. Роль QA запускає тести, ревізор перевіряє незалежно
4. Роль security перевіряє secrets
5. Роль backend/architect перевіряють відповідність вимогам
6. Підтверджені дефекти оформлюються як `lab`-issues

### Етап 7 — Повний 24/7 режим
**Мета:** активувати постійний режим лабораторії.

**Кроки:**
1. Власник активує `LAB_POLICY_GATE_ACTIVE=true`
2. Власник видає grants для дозволених gated-дій
3. Визначити ліміти бюджету на основі вимірювань
4. Вдосконалити Skills на основі результатів Етапу 6
5. Angular Web + CLI: макети визначають scope першої версії
6. Перевірити production-priority при 24/7 роботі
