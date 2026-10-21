# Чекліст Етапу 6 — Перший повноцінний аудит

**Мета:** запустити реальний аудит коду, тестів, документації, Issues та відповідності ТЗ.

**Попередні умови:**
- [ ] Етап 5 виконано (lab запущено в Docker)
- [ ] Git репозиторій клонований в `/srv/vertep-lab/repo`
- [ ] `LAB_WORKSPACE` налаштовано
- [ ] Python та тестові залежності доступні

---

## 1. Підготовка робочої копії

### 1.1 Перевірити workspace

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli status
```

Перевірити в output:
- `workspace.root` — шлях до робочої копії
- `workspace.isolated` — `true`

### 1.2 Ініціалізувати Git копію

```bash
# Перший раз
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
git clone https://github.com/fylypovych/vertep.git .
git checkout main
"

# Або якщо вже клоновано
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
git fetch origin
git reset --hard origin/main
"
```

### 1.3 Перевірити ізоляцію

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -c "
from lab.workspace import Workspace
ws = Workspace('/srv/vertep-lab/repo')
report = ws.verify()
print(report)
"
```

**Очікуваний результат:**
- `isolated: true`
- `problems: []`

---

## 2. Статичні перевірки

### 2.1 Compileall

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m compileall -q core adapters worker lab scripts installer tests
"
```

**Очікуваний результат:**
- Відсутність помилок компіляції

### 2.2 Pytest collect

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest -q --collect-only
"
```

**Очікуваний результат:**
- Відсутність collection errors
- Кількість тестів > 0

---

## 3. Запуск тестів (роль QA)

### 3.1 Повний pytest

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest -q
"
```

**Очікуваний результат:**
- Всі тести passed (крім e2e без запущеного сервера)
- Нові падіння блокують аудит

### 3.2 Lab-specific тести

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest -q tests/test_lab_*.py
"
```

**Очікуваний результат:**
- 118 passed (або більше якщо додано нові тести)

### 3.3 Coverage (опціонально)

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
pip install pytest-cov
python -m pytest --cov=lab --cov-report=term-missing tests/test_lab_*.py
"
```

**Очікуваний результат:**
- Coverage lab модулів > 80%
- Відсутність critical uncovered code

---

## 4. Перевірка secrets (роль security)

### 4.1 Grep по секретах

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
grep -r -i 'password\|secret\|token\|api_key\|private_key' --include='*.py' core/ adapters/ worker/ | grep -v 'test_\|integration_secret\|_secret'
"
```

**Очікуваний результат:**
- Відсутність hardcoded secrets в коді
- Лише `integration_secret` та `_secret` змінні (дозволені)

### 4.2 Перевірити конфігураційні файли

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
find . -name '*.env*' -o -name '*config*' -o -name '*secret*' | grep -v '.git' | grep -v '__pycache__'
"
```

**Очікуваний результат:**
- Відсутність конфігураційних файлів з секретами в репозиторії
- Лише приклади (`.env.example`)

---

## 5. Архітектурний аудит (роль architect)

### 5.1 Перевірити CORE Generation Gate

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest tests/test_core_generation_gate.py -v
"
```

**Очікуваний результат:**
- Всі тести passed
- CORE не викликає generation напряму

### 5.2 Перевірити provider/adapter layer

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
grep -r 'ComfyUIAdapter\|TTSAdapter' --include='*.py' worker/ | grep -v 'execute_role_task'
"
```

**Очікуваний результат:**
- Відсутність прямих викликів адаптерів в `worker/service.py`
- Всі виклики через `execute_role_task()`

### 5.3 Перевірити персонажів як конфіги

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
grep -r 'character.*=' --include='*.py' core/ adapters/ | grep -v 'characters/'
"
```

**Очікуваний результат:**
- Відсутність хардкод персонажів
- Персонажі лише в `/characters/`

---

## 6. Backend аудит (роль backend)

### 6.1 Перевірити pipeline

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest tests/test_pipeline.py -v
"
```

**Очікуваний результат:**
- Всі тести passed

### 6.2 Перевірити dispatcher

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest tests/test_dispatcher.py -v
"
```

**Очікуваний результат:**
- Всі тести passed

### 6.3 Перевірити API

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cd /srv/vertep-lab/repo
python -m pytest tests/test_api.py -v
"
```

**Очікуваний результат:**
- Всі тести passed

---

## 7. Незалежний ревізор (роль reviewer)

### 7.1 Перевірити результати тестів QA

Ревізор переглядає результати pytest:
- Перевірити, що всі critical тести passed
- Перевірити відсутність нових падінь
- Перевірити coverage адекватне

### 7.2 Перевірити результати security

Ревізор переглядає grep результати:
- Перевірити відсутність пропущених secrets
- Перевірити відсутність false positives

### 7.3 Перевірити результати architect/backend

Ревізор переглядає архітектурні перевірки:
- Перевірити відповідність принципам CORE Generation Gate
- Перевірити відсутність прямих викликів адаптерів
- Перевірити персонажів як конфіги

---

## 8. Оформлення lab-issues

### 8.1 Створити finding для дефекту

Приклад `lab-issue` JSON:

```json
{
  "identifier": "lab.0.0.0.1",
  "kind": "defect",
  "title": "Missing test for X function",
  "evidence": "pytest tests/test_x.py не покриває edge case Y",
  "reproduction": "python -m pytest tests/test_x.py -v",
  "risk": "mid",
  "expected_result": "test покриває edge case Y",
  "boundaries": "лише додавання тесту, без зміни логіки",
  "source": "Етап 6 аудит",
  "approved_scope": "AGENTS.md §11 — тести перед релізом"
}
```

### 8.2 Створити issue через CLI

```bash
# Включити policy gate (тимчасово для тесту)
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
export LAB_POLICY_GATE_ACTIVE=true
python -m lab.cli grant create_lab_issue --owner-token \"\$LAB_OWNER_TOKEN\" --hours 8
"

# Створити issue
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
python -m lab.cli lab-issue --finding /path/to/finding.json
"
```

### 8.3 Перевірити створення

```bash
# Перевірити lab state
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cat /var/lib/vertep-lab/governance/created-issues.json
"
```

**Очікуваний результат:**
- Issue створено
- `created: true`
- Issue number та URL присутні

---

## 9. Handoff та повторний запуск

### 9.1 Перевірити handoff

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cat /var/lib/vertep-lab/handoff.json
"
```

**Очікуваний результат:**
- `summary` містить опис виконаної роботи
- `next_steps` порожній або з подальшими задачами
- `pending_tasks` список очікуваних задач

### 9.2 Повторний запуск

```bash
# Systemd timer автоматично запустить через 15 хвилин
# Або вручну:
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli run
```

**Очікуваний результат:**
- Lab використовує handoff замість повторного читання всього репозиторію
- Новий запуск продовжує з місця, де зупинився попередній

---

## 10. Критерії приймання

- [ ] 6.1: Оркестратор створює ізольовану Git-копію
- [ ] 6.2: Статичні перевірки проходять перед AI-викликами
- [ ] 6.3: Роль QA запускає тести, ревізор перевіряє незалежно
- [ ] 6.4: Роль security перевіряє відсутність hardcoded secrets
- [ ] 6.5: Роль backend/architect перевіряють відповідність вимогам
- [ ] 6.6: Підтверджені дефекти оформлюються як `lab`-issues
- [ ] 6.7: Результати аудиту в `handoff.json` та коментарі до issue
- [ ] 6.8: Жодного автономного `i`/`ir`/`var` створено
- [ ] 6.9: Lab не публікує код без дозволу
- [ ] 6.10: Audit trail записує всі дії

---

## 11. Типові дефекти для пошуку

### 11.1 Відсутні тести

- Функції без unit тестів
- Edge cases не покриті
- Error handling не протестовано

### 11.2 Secrets

- Hardcoded API keys
- Hardcoded passwords
- Tokens в коді

### 11.3 Архітектурні порушення

- Прямі виклики ComfyUIAdapter в worker/service.py
- CORE Generation Gate порушено
- Хардкод персонажів

### 11.4 Документація

- Розбіжності між кодом та AGENTS.md
- Відсутні оновлення в CHANGELOG.md
- Невідповідність VERSION та commit

---

## 12. Звіт про аудит

Створити звіт:

```json
{
  "audit_date": "2026-10-18",
  "lab_version": "0.0.1.7",
  "commit": "abcd123",
  "tests_run": 200,
  "tests_passed": 198,
  "tests_failed": 2,
  "secrets_found": 0,
  "architectural_issues": 1,
  "lab_issues_created": 3,
  "summary": "Аудит завершено. Знайдено 2 падіння тестів, 1 архітектурну проблему, створено 3 lab-issues."
}
```

Зберегти в `/var/lib/vertep-lab/reports/audit-YYYY-MM-DD.json`.
