# Чекліст Етапу 4 — GPU Idle-Lease Інтеграція

**Мета:** підключити `LeaseManager` до реального production dispatcher та перевірити витіснення.

**Попередні умови:**
- [ ] Ubuntu VM готова (4–8 vCPU, 16–32 ГБ RAM, 100–200 ГБ SSD)
- [ ] Vertep production запущено на окремому вузлі з GPU
- [ ] Мережевий доступ між lab VM та production CORE

---

## 1. Налаштування production dispatcher

### 1.1 Створити lab token

```bash
# На production CORE
openssl rand -hex 32
```

Зберегти як `LAB_VERTEP_TOKEN` в конфігурації Vertep.

### 1.2 Реалізувати HTTP endpoint в CORE

Додати в CORE API (`core/api/` або новий модуль):

```python
# core/api/lab_lease.py
from fastapi import APIRouter, Header, HTTPException, Depends
from typing import Optional

router = APIRouter(prefix="/lab-lease/v1", tags=["lab-lease"])

@router.get("/status")
async def lease_status(
    x_vertep_lab_token: str = Header(...),
):
    # Перевірити токен
    if x_vertep_lab_token != os.getenv("LAB_VERTEP_TOKEN"):
        raise HTTPException(401, "Invalid token")

    # Отримати стан з Dispatcher
    queue_depth = dispatcher.get_queue_depth()
    gpu_busy = dispatcher.is_gpu_busy()
    free_vram = monitor.get_free_vram()

    return {
        "contract_version": "1",
        "provider": "vertep",
        "gpu_available": queue_depth == 0 and not gpu_busy,
        "free_vram_mb": free_vram,
        "queue_depth": queue_depth,
        "reserved": False,
        "gpu_busy": gpu_busy,
        "active_leases": list(active_leases.keys()),
        "reason": "",
        "idle_sharing_enabled": os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() == "true",
    }
```

### 1.3 Реалізувати lease endpoints

```python
@router.post("/lease/request")
async def request_lease(
    requirement: dict,
    ttl_seconds: int,
    x_vertep_lab_token: str = Header(...),
):
    # Перевірити токен
    if x_vertep_lab_token != os.getenv("LAB_VERTEP_TOKEN"):
        raise HTTPException(401, "Invalid token")

    # Перевірити idle sharing
    if not os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() == "true":
        return {"granted": False, "lease": None, "reason": "idle sharing is disabled by policy"}

    # Перевірити чергу
    queue_depth = dispatcher.get_queue_depth()
    if queue_depth > 0:
        return {"granted": False, "lease": None, "reason": "production queue is not empty"}

    # Перевірити GPU
    if dispatcher.is_gpu_busy():
        return {"granted": False, "lease": None, "reason": "GPU is busy with production work"}

    # Перевірити VRAM
    required_vram = requirement.get("vram_mb", 0)
    free_vram = monitor.get_free_vram()
    if required_vram > free_vram:
        return {"granted": False, "lease": None,
                "reason": f"model needs {required_vram} MB, only {free_vram} MB free"}

    # Видати lease
    lease_id = f"lease-{uuid.uuid4().hex[:8]}"
    lease = {
        "lease_id": lease_id,
        "endpoint": os.getenv("LAB_GPU_ENDPOINT", "http://localhost:11434"),
        "model": requirement.get("model"),
        "granted_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
        "heartbeat_seconds": 30,
        "state": "GRANTED",
        "vram_mb": required_vram,
    }
    active_leases[lease_id] = lease
    return {"granted": True, "lease": lease, "reason": ""}

@router.post("/lease/heartbeat")
async def lease_heartbeat(
    lease_id: str,
    ttl_seconds: int,
    x_vertep_lab_token: str = Header(...),
):
    if x_vertep_lab_token != os.getenv("LAB_VERTEP_TOKEN"):
        raise HTTPException(401, "Invalid token")

    lease = active_leases.get(lease_id)
    if not lease:
        return {"active": False, "revoked": False, "expires_at": "", "reason": "unknown lease"}

    # Перевірити відкликання
    if lease.get("revoked"):
        return {"active": False, "revoked": True, "expires_at": lease["expires_at"],
                "reason": "production work arrived: lease revoked"}

    # Перевірити expiry
    if datetime.now(timezone.utc) >= datetime.fromisoformat(lease["expires_at"]):
        return {"active": False, "revoked": False, "expires_at": lease["expires_at"],
                "reason": "lease expired"}

    # Продовжити
    lease["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    return {"active": True, "revoked": False, "expires_at": lease["expires_at"], "reason": ""}

@router.post("/lease/release")
async def release_lease(
    lease_id: str,
    x_vertep_lab_token: str = Header(...),
):
    if x_vertep_lab_token != os.getenv("LAB_VERTEP_TOKEN"):
        raise HTTPException(401, "Invalid token")

    lease = active_leases.pop(lease_id, None)
    if not lease:
        return {"released": False, "reason": "unknown lease"}

    lease["state"] = "RELEASED"
    return {"released": True, "reason": "", "free_vram_mb": monitor.get_free_vram()}
```

### 1.4 Інтегрувати в Dispatcher

Додати логіку відкликання lease при появі production роботи:

```python
# core/dispatcher.py
def on_job_queued(self):
    """Callback при додаванні задачі в чергу."""
    # Відкликати всі lab leases
    for lease_id in list(active_leases.keys()):
        active_leases[lease_id]["revoked"] = True
        logger.info(f"Lab lease {lease_id} revoked due to production work")
```

---

## 2. Налаштування lab VM

### 2.1 Встановити змінні середовища

```bash
# На lab VM
export LAB_VERTEP_URL="https://vertep-core.example.com/lab-lease/v1"
export LAB_VERTEP_TOKEN="..."
export LAB_IDLE_SHARING_ENABLED="true"
```

Додати в `/opt/vertep-lab/lab.env`:

```bash
LAB_VERTEP_URL=https://vertep-core.example.com/lab-lease/v1
LAB_VERTEP_TOKEN=...
LAB_IDLE_SHARING_ENABLED=true
```

### 2.2 Перевірити мережевий доступ

```bash
curl -H "X-Vertep-Lab-Token: $LAB_VERTEP_TOKEN" \
     -H "X-Vertep-Contract-Version: 1" \
     "$LAB_VERTEP_URL/status"
```

Очікуваний результат:

```json
{
  "contract_version": "1",
  "gpu_available": true,
  "free_vram_mb": 24576,
  ...
}
```

---

## 3. Перевірки контракту

### 3.1 GET /status

```bash
python -m lab.cli gpu status
```

**Очікуваний результат:**
- `contract_version` = "1"
- `gpu_available` = true коли GPU вільний
- `free_vram_mb` > 0

### 3.2 POST /lease/request

```bash
python -m lab.cli gpu request --model qwen2.5-coder:7b --vram-mb 8192
```

**Очікуваний результат:**
- `granted: true` коли GPU вільний
- `lease` містить `lease_id`, `endpoint`, `expires_at`
- `granted: false` коли GPU зайнятий

### 3.3 Перевірка відмови при черзі

На production Vertep:

```bash
# Створити тестову задачу
curl -X POST http://localhost:8080/api/jobs -d '{"title": "test"}'
```

На lab VM:

```bash
python -m lab.cli gpu request --model qwen2.5-coder:7b --vram-mb 8192
```

**Очікуваний результат:**
- `granted: false`
- `reason: "production queue is not empty"`

### 3.4 POST /lease/heartbeat

```bash
python -m lab.cli gpu heartbeat
```

**Очікуваний результат:**
- `active: true` коли lease дійсний
- `expires_at` оновлено

### 3.5 Перевірка відкликання

На production Vertep:
- Симулювати появу production роботи (додати задачу в чергу)

На lab VM:
```bash
python -m lab.cli gpu heartbeat
```

**Очікуваний результат:**
- `active: false`
- `revoked: true`
- `reason: "production work arrived: lease revoked"`

### 3.6 POST /lease/release

```bash
python -m lab.cli gpu release
```

**Очікуваний результат:**
- `released: true`
- `free_vram_mb` оновлено

### 3.7 Перевірка _restore після краху

```bash
# Змусити lease залишитися
python -m lab.cli gpu request --model qwen2.5-coder:7b --vram-mb 8192

# Зупинити lab без release
kill -9 $(pgrep -f "lab.cli")

# Перезапустити lab
python -m lab.cli gpu status
```

**Очікуваний результат:**
- Старий lease відстрочено автоматично в `_restore()`
- Новий lease можна отримати

### 3.8 Перевірка звільнення в finally

Змусити `PolicyDenied` під час lease:

```bash
# Спробувати дію без grant
python -m lab.cli check create_ir_issue --subject test
```

Перевірити в логах lab:
- Lease звільнено в `finally` блоку `run_once()`

---

## 4. Тестовий сценарій

### 4.1 Повний цикл

```bash
# 1. Статус
python -m lab.cli gpu status

# 2. Запит lease
python -m lab.cli gpu request --model qwen2.5-coder:7b --vram-mb 8192

# 3. Heartbeat (кілька разів)
for i in {1..5}; do
    python -m lab.cli gpu heartbeat
    sleep 30
done

# 4. Звільнити
python -m lab.cli gpu release

# 5. Перевірити статус
python -m lab.cli gpu status
```

### 4.2 Сценарій витіснення

```bash
# 1. Отримати lease
python -m lab.cli gpu request --model qwen2.5-coder:7b --vram-mb 8192

# 2. На production: додати задачу в чергу

# 3. Heartbeat на lab
python -m lab.cli gpu heartbeat
# Очікується: revoked: true

# 4. Звільнити
python -m lab.cli gpu release
```

---

## 5. Критерії приймання

- [ ] 4.1: Production endpoint доступний з lab VM
- [ ] 4.2: `LAB_VERTEP_TOKEN` валідний
- [ ] 4.3: `GET /status` повертає правильний contract_version
- [ ] 4.4: `POST /lease/request` здає lease коли GPU вільний
- [ ] 4.5: `POST /lease/request` відмовляє коли queue_depth > 0
- [ ] 4.6: `POST /lease/heartbeat` продовжує lease
- [ ] 4.7: Production може відкликати lease
- [ ] 4.8: Lab отримує `revoked: true` при відклику
- [ ] 4.9: Lab звільняє lease при `LeaseLost`
- [ ] 4.10: `_restore` відстрочує старий lease після рестарту
- [ ] 4.11: `finally` блок гарантує звільнення

---

## 6. Логування

Включити логування в lab:

```bash
export LAB_LOG_LEVEL=DEBUG
python -m lab.cli gpu status
```

Перевірити логи:

```bash
tail -f /var/lib/vertep-lab/audit.jsonl
```

Кожен lease request, heartbeat, release має бути записаний.
