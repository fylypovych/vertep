# Профілі агентних ролей для лабораторії (Етап 2)

## Визначення ролей

На основі Issue #107 та існуючого `code-error-checker.agent.md` визначено 8 ролей:

1. **architect** — архітектурний огляд, відповідність принципам
2. **backend** — перевірка бекенд-коду, API, інтеграцій
3. **ai_gpu** — AI/GPU-інтеграції, ComfyUI, Ollama
4. **frontend** — Web UI, Angular, CLI UX
5. **devops** — Docker, deployment, інфраструктура
6. **qa** — написання/перевірка тестів, coverage
7. **security** — безпека, secrets, авторизація
8. **reviewer** — незалежний ревізор (read-only)

---

## Профілі ролей

### 1. architect

**Мета:** перевіряти архітектурну відповідність Vertep.

**STANDING_PERMISSIONS:**
- `read_state` — читання конфігурації, документації
- `static_checks` — запуск compileall, статичний аналіз
- `write_report` — створення звітів архітектурного аудиту

**GATED_ACTIONS (потребують grant):**
- `create_lab_issue` — створення `lab` для архітектурних дефектів

**Tools:**
- read, grep, glob — читання коду, пошук патернів
- search — пошук залежностей, використання модулів

**Skills:**
- MCP: не потрібні
- Локальні: архітектурний аналіз коду без змін

---

### 2. backend

**Мета:** перевіряти бекенд-код, API, CORE, worker, адаптери.

**STANDING_PERMISSIONS:**
- `read_state` — читання коду бекенду
- `static_checks` — запуск compileall, mypy якщо доступний
- `run_tests` — запуск бекенд-тестів
- `local_fix` — локальні виправлення бекенд-коду
- `write_report` — звіти про дефекти бекенду

**GATED_ACTIONS:**
- `create_lab_issue` — створення `lab` для бекенд-дефектів

**Tools:**
- read, grep, glob — читання коду
- exec — запуск тестів, компіляції

**Skills:**
- MCP: не потрібні
- Локальні: запуск pytest, перевірка логіки

---

### 3. ai_gpu

**Мета:** перевіряти AI/GPU-інтеграції: ComfyUI, Ollama, LLM-виклики.

**STANDING_PERMISSIONS:**
- `read_state` — читання коду AI/GPU модулів
- `static_checks` — перевірка коду інтеграцій
- `request_gpu_lease` — запит lease для тестування GPU-інтеграцій
- `write_report` — звіти про дефекти AI/GPU

**GATED_ACTIONS:**
- `create_lab_issue` — створення `lab` для AI/GPU дефектів

**Tools:**
- read, grep, glob — читання коду адаптерів, provider-ів
- exec — запуск тестів GPU-коду без реальної GPU

**Skills:**
- MCP: не потрібні
- Локальні: перевірка контрактів, error handling

---

### 4. frontend

**Мета:** перевіряти Web UI, Angular, CLI UX.

**STANDING_PERMISSIONS:**
- `read_state` — читання frontend-коду
- `static_checks` — перевірка TypeScript/HTML/CSS
- `write_report` — звіти про UI дефекти

**GATED_ACTIONS:**
- `create_lab_issue` — створення `lab` для UI дефектів

**Tools:**
- read, grep, glob — читання frontend-файлів
- exec — запуск lint/typecheck якщо доступний

**Skills:**
- MCP: не потрібні
- Локальні: перевірка UX патернів, accessibility

---

### 5. devops

**Мета:** перевіряти Docker, deployment, інфраструктуру.

**STANDING_PERMISSIONS:**
- `read_state` — читання Dockerfile, docker-compose, systemd-файлів
- `static_checks` — перевірка конфігурацій
- `write_report` — звіти про інфраструктурні дефекти

**GATED_ACTIONS:**
- `create_lab_issue` — створення `lab` для DevOps дефектів

**Tools:**
- read, grep, glob — читання конфігурацій
- exec — перевірка синтаксису YAML, systemd

**Skills:**
- MCP: не потрібні
- Локальні: перевірка безпеки конфігурацій, volumes

---

### 6. qa

**Мета:** написання/перевірка тестів, coverage, edge cases.

**STANDING_PERMISSIONS:**
- `read_state` — читання коду та існуючих тестів
- `static_checks` — запуск `pytest --collect`
- `run_tests` — запуск тестів
- `local_fix` — додавання/виправлення тестів
- `write_report` — звіти про test gaps

**GATED_ACTIONS:**
- `create_lab_issue` — створення `lab` для критичних test gaps

**Tools:**
- read, grep, glob — читання коду та тестів
- exec — запуск pytest, coverage

**Skills:**
- MCP: не потрібні
- Локальні: написання unit/integration тестів

---

### 7. security

**Мета:** перевірка безпеки, secrets, авторизації.

**STANDING_PERMISSIONS:**
- `read_state` — читання коду безпеки
- `static_checks` — grep по secrets, паролях, ключах
- `write_report` — звіти про security issues

**GATED_ACTIONS:**
- `create_lab_issue` — створення `lab` для security дефектів

**Tools:**
- read, grep, glob — пошук секретів, токенів
- exec — запуск security-інструментів якщо доступний

**Skills:**
- MCP: не потрібні
- Локальні: перевірка hardcoded secrets, авторизації

---

### 8. reviewer (незалежний ревізор)

**Мета:** незалежний read-only огляд змін, перевірка відповідності критеріям.

**STANDING_PERMISSIONS:**
- `read_state` — читання всього коду
- `static_checks` — запуск статичних перевірок
- `write_report` — незалежний звіт

**GATED_ACTIONS:**
- ЖОДНИХ — ревізор лише читає та звітує

**Tools:**
- read, grep, glob — read-only доступ
- exec — запуск read-only перевірок

**Skills:**
- MCP: не потрібні
- Локальні: read-only аудит

**Важливо:** ревізор не має права `local_fix` — це розділяє автора змін від оцінювача.

---

## Розділення QA ≠ ревізор

- **QA** може писати та виправляти тести (`local_fix`), тому не є незалежним оцінювачем своєї роботи.
- **Reviewer** має лише read-only доступ, не може змінювати код — це забезпечує незалежність.

---

## Виклик ролей в Orchestrator

`Orchestrator.run_once` викликає ролі послідовно:
1. Завантажує конфігурацію ролі з `role_profiles.md`
2. Створює контекст з відповідними `STANDING_PERMISSIONS`
3. Для `GATED_ACTIONS` перевіряє grant через `PolicyGate.require()`
4. Виконує роботу в межах дозволених дій
5. Звільняє ресурси (GPU lease, бюджет)

Одна роль ≠ постійно запущена модель — кожна роль викликається за потреби в межах одного `run_once`.

---

## Skills (MCP)

За принципом найменших привілеїв MCP не потрібні для жодної ролі на цьому етапі:
- Усі перевірки локальні (read code, run tests, static analysis)
- GPU доступ тільки через lease контракт
- GitHub доступ тільки через `governance.py` за gate

MCP додаватиметься лише якщо буде доведена потреба в зовнішніх інструментах, які не можна реалізувати локально.
