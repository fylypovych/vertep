# GPU Idle-Lease Contract між лабораторією та Vertep Production

**Версія контракту:** 1
**Джерело:** Issue #107 (var.0.0.1.7), §4.6

## Огляд

Лабораторія Vertep може використовувати GPU production-вузла лише під час простою через узгоджений контракт idle-lease. Vertep має абсолютний пріоритет — при появі production роботи лабораторія негайно звільняє GPU.

## Принципи

1. **Fail-closed:** без дійсного lease лабораторія не має доступу до GPU
2. **Production priority:** Vertep може відкликати lease в будь-який момент
3. **Безпечне звільнення:** лабораторія гарантує звільнення VRAM перед завершенням
4. **Відновлення після краху:** після рестарту старий lease відстрочується перед новим
5. **Ізоляція:** лабораторія не імпортує production CORE, не торкається Redis/PostgreSQL/Docker socket

## Контракт API

### Base URL

```
{LAB_VERTEP_URL}/lab-lease/v1
```

Наприклад: `https://vertep-core.example.com/lab-lease/v1`

### Заголовки

```
X-Vertep-Contract-Version: 1
X-Vertep-Lab-Token: {LAB_VERTEP_TOKEN}
Content-Type: application/json
```

### Ендпоінти

#### 1. GET /status

**Опис:** інформація про стан GPU та доступність lease.

**Request:**
```
GET /lab-lease/v1/status
```

**Response (200 OK):**
```json
{
  "contract_version": "1",
  "provider": "vertep",
  "gpu_available": true,
  "free_vram_mb": 24576,
  "queue_depth": 0,
  "reserved": false,
  "gpu_busy": false,
  "active_leases": [],
  "reason": "",
  "idle_sharing_enabled": true
}
```

**Response (503 Service Unavailable):**
```json
{
  "error": "resource endpoint unreachable",
  "details": "..."
}
```

**Поля:**
- `contract_version` — версія контракту (має бути "1")
- `gpu_available` — чи доступний GPU для lab lease
- `free_vram_mb` — вільна VRAM в MB
- `queue_depth` — кількість задач у черзі Vertep
- `reserved` — чи GPU зарезервовано для production
- `gpu_busy` — чи GPU зайнятий production роботою
- `active_leases` — список активних lease IDs
- `idle_sharing_enabled` — чи увімкнено idle sharing в конфігурації Vertep

---

#### 2. POST /lease/request

**Опис:** запит на GPU lease.

**Request:**
```json
{
  "requirement": {
    "model": "qwen2.5-coder:7b",
    "vram_mb": 8192,
    "kind": "llm_inference",
    "max_seconds": 900
  },
  "ttl_seconds": 900
}
```

**Response (200 OK, granted):**
```json
{
  "granted": true,
  "lease": {
    "lease_id": "lease-0001",
    "endpoint": "http://gpu.local:11434",
    "model": "qwen2.5-coder:7b",
    "granted_at": "2026-10-18T12:00:00Z",
    "expires_at": "2026-10-18T12:15:00Z",
    "heartbeat_seconds": 30,
    "state": "GRANTED",
    "vram_mb": 8192
  },
  "reason": ""
}
```

**Response (200 OK, denied):**
```json
{
  "granted": false,
  "lease": null,
  "reason": "production queue is not empty"
}
```

**Поля lease:**
- `lease_id` — унікальний ідентифікатор lease
- `endpoint` — URL для запитів inference (наприклад Ollama)
- `model` — модель для виконання
- `granted_at` — час видачі lease
- `expires_at` — час закінчення lease
- `heartbeat_seconds` — інтервал heartbeat (типово 30-60s)
- `state` — `GRANTED` при видачі
- `vram_mb` — виділена VRAM

**Причини відмови:**
- `production queue is not empty` — черга Vertep не пуста
- `GPU is reserved for production` — GPU зарезервовано
- `GPU is busy with production work` — GPU зайнятий
- `model needs X MB, only Y MB free` — недостатньо VRAM

---

#### 3. POST /lease/heartbeat

**Опис:** продовження lease (heartbeat).

**Request:**
```json
{
  "lease_id": "lease-0001",
  "ttl_seconds": 900
}
```

**Response (200 OK, active):**
```json
{
  "active": true,
  "revoked": false,
  "expires_at": "2026-10-18T12:30:00Z",
  "reason": ""
}
```

**Response (200 OK, revoked):**
```json
{
  "active": false,
  "revoked": true,
  "expires_at": "2026-10-18T12:15:00Z",
  "reason": "production work arrived: lease revoked"
}
```

**Response (200 OK, expired):**
```json
{
  "active": false,
  "revoked": false,
  "expires_at": "2026-10-18T12:15:00Z",
  "reason": "lease expired"
}
```

**Логіка відкликання:**
- Vertep відкликає lease при появі production роботи
- Лабораторія повинна негайно зупинити inference при `revoked: true`
- Відкликаний lease не можна поновити — треба запитувати новий

---

#### 4. POST /lease/release

**Опис:** підтвердження звільнення GPU.

**Request:**
```json
{
  "lease_id": "lease-0001"
}
```

**Response (200 OK):**
```json
{
  "released": true,
  "reason": "",
  "free_vram_mb": 24576
}
```

**Response (200 OK, unknown lease):**
```json
{
  "released": false,
  "reason": "unknown lease"
}
```

**Ідемпотентність:**
- Звільнення вже звільненого lease не є помилкою
- Vertep має повертати `released: true` якщо GPU вільний

---

## Реалізація на стороні Vertep Production

### Інтеграція в CORE/Dispatcher

1. **Новий HTTP endpoint** `/lab-lease/v1/*` в CORE API
2. **Перевірка токена** через `X-Vertep-Lab-Token`
3. **Перевірка черги** через Dispatcher
4. **Перевірка VRAM** через GPU monitoring
5. **Управлення lease** в пам'яті або Redis

### Логіка вирішення

При `/lease/request`:

```
IF NOT idle_sharing_enabled:
    DENY "idle sharing is disabled by policy"

IF queue_depth > 0:
    DENY "production queue is not empty"

IF gpu_reserved:
    DENY "GPU is reserved for production"

IF gpu_busy:
    DENY "GPU is busy with production work"

IF requirement.vram_mb > free_vram_mb:
    DENY "model needs X MB, only Y MB free"

GRANT lease
```

### Логіка відкликання

При появі production роботи:

```
FOR EACH active_lab_lease:
    Mark lease as REVOKING
    Notify lab via next heartbeat
    Reclaim VRAM after safe timeout (30-60s)
```

### Безпека

1. **Token validation:**
   - Токен зберігається в `LAB_VERTEP_TOKEN` environment variable
   - Токен перевіряється при кожному запиті
   - Недійсний токен → 401 Unauthorized

2. **Rate limiting:**
   - Обмежити кількість lease requests з одного IP
   - Обмежити частоту heartbeat

3. **Audit logging:**
   - Логувати всі lease requests, grants, revokes
   - Включати timestamp, lab_token (маскований), reason

---

## Реалізація на стороні Лабораторії

### VertepResourceClient

Модуль `lab/gpu_lease.py` вже містить `VertepResourceClient`:

```python
from lab.gpu_lease import VertepResourceClient, ResourceRequirement, LeaseManager

client = VertepResourceClient(
    base_url=os.getenv("LAB_VERTEP_URL"),
    token=os.getenv("LAB_VERTEP_TOKEN"),
    enabled=os.getenv("LAB_IDLE_SHARING_ENABLED", "false").lower() == "true"
)

manager = LeaseManager(client, state_dir=state_dir, actor="lab")
```

### Захист від краху

`LeaseManager._restore()` викликається при старті:

```python
def _restore(self):
    if self._state_path.exists():
        record = read_json(self._state_path)
        if record.get("lease_id"):
            self.release(record["lease_id"], reason="restart reconciliation")
```

Це гарантує, що старий lease не залишається "висять" після краху.

---

## Перевірки на реальному стенді (Етап 4)

### Checklist

1. [ ] Production endpoint доступний з lab VM
2. [ ] `LAB_VERTEP_TOKEN` валідний
3. [ ] `GET /status` повертає правильний contract_version
4. [ ] `POST /lease/request` здає lease коли GPU вільний
5. [ ] `POST /lease/request` відмовляє коли queue_depth > 0
6. [ ] `POST /lease/heartbeat` продовжує lease
7. [ ] Production може відкликати lease (simulate_production_work)
8. [ ] Lab отримує `revoked: true` при відклику
9. [ ] Lab звільняє lease при `LeaseLost`
10. [ ] `_restore` відстрочує старий lease після рестарту
11. [ ] `finally` блок гарантує звільнення

### Тестовий сценарій

```bash
# На lab VM
export LAB_VERTEP_URL="https://vertep-core.example.com/lab-lease/v1"
export LAB_VERTEP_TOKEN="..."
export LAB_IDLE_SHARING_ENABLED="true"

python -m lab.cli gpu status
python -m lab.cli gpu request --model qwen2.5-coder:7b --vram-mb 8192
python -m lab.cli gpu heartbeat
python -m lab.cli gpu release
```

---

## Зворотна сумісність

Якщо Vertep змінить контракт:

1. Змінити `contract_version` у `/status`
2. Лабораторія відмовляє запити з невідповідною версією
3. Нову версію контракту реалізувати як `VertepResourceClient` v2
4. Старий клієнт продовжує працювати поки Vertep підтримує v1
