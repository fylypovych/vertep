# Чекліст Етапу 7 — Повний 24/7 Режим

**Мета:** активувати постійний режим лабораторії та вдосконалити Skills.

**Попередні умови:**
- [ ] Етапи 4–6 виконано
- [ ] GPU lease інтегровано та протестовано
- [ ] Lab запущено в Docker та працює стабільно
- [ ] Перший аудит виконано та lab-issues створено

---

## 1. Активація Policy Gate

### 1.1 Увімкнути policy gate

```bash
# Редагувати /opt/vertep-lab/lab.env
sudo sed -i 's/LAB_POLICY_GATE_ACTIVE=false/LAB_POLICY_GATE_ACTIVE=true/' /opt/vertep-lab/lab.env
```

### 1.2 Перезапустити lab

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml restart lab-control
```

### 1.3 Перевірити активацію

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli status
```

**Очікуваний результат:**
- `policy.activated: true`

---

## 2. Видача Grants

### 2.1 Grant для create_lab_issue

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
python -m lab.cli grant create_lab_issue \
  --subject lab.0.0.0.1 \
  --approved-by owner \
  --hours 24
"
```

### 2.2 Grant для create_ir_issue (якщо потрібно)

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
python -m lab.cli grant create_ir_issue \
  --subject ir.0.0.0.1 \
  --approved-by owner \
  --hours 24
"
```

### 2.3 Перевірити активні grants

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli status | jq '.policy.active_grants'
```

**Очікуваний результат:**
- Список grants з `revoked: false`
- `expires_at` в майбутньому

---

## 3. Налаштування бюджету

### 3.1 Визначити початкові ліміти

На основі вимірювань Етапу 6:

```bash
# Якщо аудит займав 30 хвилин і використав 50k tokens
# Встановити:
# max_runs_per_day = 48 (кожні 30 хвилин)
# max_runs_per_hour = 4
# max_tokens_per_day = 400k (запас)
```

### 3.2 Налаштувати бюджет

```bash
# Редагувати budget.json напряму або через CLI
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
python -c "
from lab.budget import BudgetLedger
from pathlib import Path
budget = BudgetLedger(Path('/var/lib/vertep-lab/budget'))
budget.max_runs_per_day = 48
budget.max_runs_per_hour = 4
budget.max_tokens_per_day = 400000
budget._save_limits()
"
```

### 3.3 Перевірити бюджет

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli status | jq '.budget'
```

**Очікуваний результат:**
- Ліміти налаштовані
- `runs_last_day` та `tokens_last_day` < лімітів

---

## 4. Налаштування GPU lease (якщо Етап 4 виконано)

### 4.1 Увімкнути idle sharing

```bash
# Редагувати /opt/vertep-lab/lab.env
sudo sed -i 's/LAB_IDLE_SHARING_ENABLED=false/LAB_IDLE_SHARING_ENABLED=true/' /opt/vertep-lab/lab.env
```

### 4.2 Перезапустити lab

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml restart lab-control
```

### 4.3 Перевірити GPU статус

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli gpu status
```

**Очікуваний результат:**
- `gpu_available: true` коли GPU вільний
- `idle_sharing_enabled: true`

---

## 5. Налаштування Systemd Timer

### 5.1 Змінити інтервал (за потреби)

```bash
# Редагувати /etc/systemd/system/vertep-lab.timer
sudo nano /etc/systemd/system/vertep-lab.timer
```

Приклад (кожні 5 хвилин):

```ini
[Timer]
OnCalendar=*:0/5
RandomizedDelaySec=60
Persistent=true
```

### 5.2 Перезавантажити systemd

```bash
sudo systemctl daemon-reload
sudo systemctl restart vertep-lab.timer
```

### 5.3 Перевірити таймер

```bash
sudo systemctl list-timers vertep-lab.timer
```

**Очікуваний результат:**
- `NEXT` показує наступний запуск
- `LEFT` показує час до наступного запуску

---

## 6. Моніторинг 24/7

### 6.1 Перевірити логи

```bash
# Systemd
sudo journalctl -u vertep-lab.service -f

# Docker
docker compose -f /opt/vertep-lab/docker-compose.yml logs -f lab-control
```

### 6.2 Перевірити audit trail

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
tail -f /var/lib/vertep-lab/audit.jsonl
"
```

### 6.3 Перевірити бюджет

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cat /var/lib/vertep-lab/budget/runs.jsonl | tail -10
"
```

### 6.4 Перевірити GPU lease

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control bash -c "
cat /var/lib/vertep-lab/lease.json
"
```

---

## 7. Перевірка Production Priority

### 7.1 Симулювати production навантаження

На production Vertep:
- Додати кілька задач в чергу
- Запустити GPU-інтенсивну роботу

### 7.2 Перевірити lab поведение

```bash
# Чекати наступний запуск lab (за таймером)
# Або вручну:
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli run
```

**Очікуваний результат:**
- Lab отримує `LeaseDenied` або `LeaseLost`
- Lab відступає і не споживає GPU
- Lab переходить на fallback (зовнішня безкоштовна модель або пауза)

### 7.3 Перевірити відновлення після навантаження

Коли production черга спадає:
```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli run
```

**Очікуваний результат:**
- Lab успішно отримує lease
- Lab продовжує роботу

---

## 8. Angular Web + CLI (майбутнє)

Цей етап вимагає окремої реалізації Angular Web панелі. На даний момент:

### 8.1 Макети (mockups)

Створити макети Web панелі:
- Dashboard стану (policy, budget, GPU, queue)
- Список lab/i/ir/var issues
- Центр погоджень (grants, закриття, консолідація)
- Статус GPU/моделей
- Історія рішень (audit trail)

### 8.2 API integration

Web панель використовує ті самі endpoint, що CLI:
- `GET /api/lab/status`
- `GET /api/lab/audit`
- `POST /api/lab/check`
- `POST /api/lab/grants`
- `POST /api/lab/run`
- `GET /api/lab/gpu`
- `POST /api/lab/issues/lab`

### 8.3 Сповіщення

Налаштувати сповіщення:
- Web панель + Telegram
- Налаштування важливості (critical, warning, info)
- Звіти — комбіновані (email + web + telegram)

---

## 9. Вдосконалення Skills

### 9.1 Аналіз результатів Етапу 6

Переглянути:
- Які дефекти знайдено найчастіше
- Які модулі потребують найбільше уваги
- Які тести найчастіше падають

### 9.2 Налаштування MCP (за потреби)

Якщо виявлена потреба в зовнішніх інструментах:
- Додати MCP сервер для конкретної ролі
- Обмежити права MCP (найменші привілеї)
- Протестувати в ізольованому середовищі

### 9.3 Оптимізація pipeline

Зменшити час між запусками:
- Кешувати результати статичних перевірок
- Паралелізувати незалежні тести
- Використовувати incremental тестування

---

## 10. Критерії приймання

- [ ] 7.1: Власник активував `LAB_POLICY_GATE_ACTIVE=true`
- [ ] 7.2: Власник видав grants для дозволених gated-дій
- [ ] 7.3: Ліміти бюджету визначені на основі вимірювань
- [ ] 7.4: BudgetLedger налаштований та працює
- [ ] 7.5: Systemd timer працює стабільно
- [ ] 7.6: Lab працює 24/7 без втручання
- [ ] 7.7: Production-priority дотримано при навантаженні
- [ ] 7.8: Lab відступає при відклику GPU lease
- [ ] 7.9: Audit trail записує всі дії
- [ ] 7.10: Графіки моніторингу налаштовані (prometheus/grafana — опціонально)

---

## 11. Резервне копіювання

### 11.1 Backup state directory

```bash
# Cron job для щоденного backup
0 2 * * * tar -czf /backup/vertep-lab-state-$(date +\%Y\%m\%d).tar.gz /var/lib/vertep-lab
```

### 11.2 Backup reports

```bash
# Копіювати звіти аудиту
0 3 * * * cp -r /var/lib/vertep-lab/reports /backup/vertep-lab-reports-$(date +\%Y\%m\%d)
```

### 11.3 Перевірити відновлення

```bash
# Тестове відновлення
tar -xzf /backup/vertep-lab-state-YYYY-MM-DD.tar.gz -C /tmp/vertep-lab-restore
docker compose -f /opt/vertep-lab/docker-compose.yml down
cp -r /tmp/vertep-lab-restore/* /var/lib/vertep-lab/
docker compose -f /opt/vertep-lab/docker-compose.yml up -d
```

---

## 12. Зупинка 24/7 режиму

### 12.1 Зупинити timer

```bash
sudo systemctl stop vertep-lab.timer
sudo systemctl disable vertep-lab.timer
```

### 12.2 Зупинити lab

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml down
```

### 12.3 Зберегти state

```bash
# Копіювати state для аналізу
cp -r /var/lib/vertep-lab /backup/vertep-lab-final-state
```

---

## 13. Діагностика проблем

### 13.1 Lab не запускається

Перевірити:
```bash
sudo journalctl -u vertep-lab.service -n 50
docker compose -f /opt/vertep-lab/docker-compose.yml logs lab-control
```

### 13.2 Policy gate блокує все

Перевірити:
```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli status | jq '.policy'
```

Перевірити:
- `activated: true`
- Активні grants існують

### 13.3 Budget вичерпано

Перевірити:
```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli status | jq '.budget'
```

Рішення:
- Збільшити ліміти
- Чекати до поновлення дня
- Вручну скинути бюджет

### 13.4 GPU lease не працює

Перевірити:
```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control python -m lab.cli gpu status
```

Перевірити:
- `LAB_VERTEP_URL` правильний
- `LAB_VERTEP_TOKEN` валідний
- Production endpoint доступний
- Idle sharing увімкнено

---

## 14. Моніторинг з Prometheus (опціонально)

### 14.1 Експорт метрик

Додати Prometheus exporter в lab API:

```python
# lab/api.py
from prometheus_client import make_asgi_app

metrics_app = make_asgi_app()

@app.get("/metrics")
def metrics():
    return metrics_app
```

### 14.2 Метрики для моніторингу

- `lab_runs_total` — загальна кількість запусків
- `lab_runs_failed_total` — кількість failed запусків
- `lab_budget_tokens_used` — використані tokens
- `lab_gpu_lease_duration_seconds` — тривалість lease
- `lab_policy_denied_total` — кількість denied рішень

### 14.3 Grafana dashboard

Створити dashboard:
- Кількість запусків за годину/день
- Використання бюджету
- GPU lease статистика
- Policy gate decisions (allowed/denied)
- Audit trail entries

---

## 15. Production readiness

Перед переходом до production:

- [ ] Всі етапи 4–6 пройдені на реальному стенді
- [ ] Lab працює стабільно 24/7 протягом 7 днів
- [ ] Відсутності critical падінь
- [ ] GPU lease інтеграція працює коректно
- [ ] Audit trail цілісний
- [ ] Backup та restore протестовані
- [ ] Моніторинг налаштовано
- [ ] Документація актуалізована
