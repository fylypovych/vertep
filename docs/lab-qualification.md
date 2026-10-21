# Кваліфікація лабораторії — докази R1–R6 (Issue #252, `ir.0.0.2.52`)

**Мета:** зібрати перевірювані докази реальної кваліфікації лабораторії на окремій
Ubuntu Server 24.04 VM з production-priority Dispatcher/GPU та перевірити їх
машино-читним валідатором.

**Межі:** цей документ описує *процедуру та інструмент збору доказів*. Підготовка
процедури, конфігурації та collector'а **не є PASS R1–R6**. Рядок набуває PASS лише
тоді, коли його факти виміряні на реальному стенді, а документ доказів пройшов
незалежну верифікацію.

---

## 1. Передумови

### 1.1 Стенд

| Передумова | Значення | Навіщо |
| --- | --- | --- |
| OS | Ubuntu Server 24.04 LTS | R1 |
| vCPU | 4–8 | R1 |
| RAM | 16–32 GB | R1 |
| SSD | 100–200 GB | R1 |
| Docker + systemd | окрема інсталяція лабораторії | R1 |
| GPU | фізичний, production-priority, **без passthrough у lab-контейнер** | R2, R3 |
| Мережа | egress лише за allowlist | R1 |

Відхилення від envelope ресурсів допускаються лише за погодженням власника:
у каталог доказів кладеться `r1-resource-approval.json`, який collector хешує і
враховує.

### 1.2 Права власника, без яких рядки лишаються `BLOCKED`

- `LAB_VERTEP_URL` — endpoint production-priority ресурсів (R2, R3);
- `LAB_IDLE_SHARING_ENABLED=true` — явна активація GPU-шаріння (R2);
- `LAB_ROLE_EXECUTOR_URL` + `LAB_ROLE_EXECUTOR_ENABLED=true` — реальні model routes (R4);
- `LAB_WORKSPACE` — ізольована Git copy на VM (R4);
- `LAB_POLICY_GATE_ACTIVE=true` та grants — 24/7 режим (R5);
- `LAB_QUALIFICATION_VERIFIER` або `verification.json` — незалежна верифікація (R6).

Жодна з цих змін не виконується автоматично: collector лише читає конфігурацію і
повідомляє, чого не вистачає.

---

## 2. Команди

```bash
# Каталог рядків і відсутні передумови (план — не доказ, код 1)
python scripts/qualify_lab.py --plan

# Збір реальних доказів на стенді (код 0 лише за повного PASS)
python scripts/qualify_lab.py --collect \
    --evidence-dir /opt/vertep-lab/qualification \
    --output /opt/vertep-lab/qualification/evidence.json

# Незалежна перевірка вже зібраного документа (код 0 лише за повного PASS)
python scripts/qualify_lab.py --validate /opt/vertep-lab/qualification/evidence.json
```

Каталог доказів за замовчуванням — поза робочим деревом репозиторію
(`LAB_QUALIFICATION_EVIDENCE_DIR`, інакше `/opt/vertep-lab/qualification`).

---

## 3. Процедура по рядках

### R1 — ізольована VM, ресурси, життєвий цикл

1. На VM виконати незалежні `start` / `stop` / `restart` лабораторії
   (`systemctl`/`docker compose`) і зберегти вивід у
   `<evidence>/r1-lifecycle.txt`.
2. Переконатися, що всередині контейнера доступний `status`.
3. Виконати `python scripts/qualify_lab.py --collect` — collector виміряє
   CPU/RAM/SSD, OS, контейнерний режим, Docker, негативні ізоляційні проби
   (`/var/run/docker.sock` нечитний, `/opt/vertep/*`, `/data/*`, `/etc/vertep`
   незаписні), allowlist мережі та відсутність GPU passthrough.

Row `PASS` вимагає: Ubuntu 24.04, ресурси в envelope (або погоджене відхилення),
жодної доступної production-точки, налаштований allowlist, відсутність
passthrough та артефакт життєвого циклу.

### R2 — lease на production-priority GPU

1. Надати `LAB_VERTEP_URL` та увімкнути `LAB_IDLE_SHARING_ENABLED` (owner).
2. Під час виконання `--collect` production має надіслати власний task, щоб
   laboratory побачила revoke. Collector виміряє: deny без lease, стан endpoint'а
   під час lease (queue/reservation), heartbeat latency, revoke/control-stop,
   release ack та VRAM до/після release.
3. Якщо production task не надійшов під час вікна вимірювання — revoke-шлях не
   спостерігався, рядок лишається `FAIL`/`BLOCKED`, а не `PASS`.

### R3 — fault-сценарії

1. Collector сам вимірює: `connection_loss` (недоступний endpoint → fail-closed),
   відсутність GPU без lease, реконфіляція збереженого lease після restart,
   звільнення lease у `finally`.
2. Реальні `inference hang`, `VM/Dispatcher restart` і `quota exhaustion`
   відтворює оператор на стенді; результат фіксується у
   `<evidence>/r3-hang.json`, `r3-restart.json`, `r3-quota.json` — collector
   хешує ці артефакти і не приймає рядок без них.

### R4 — перший actual audit

1. Надати ізольовану Git copy (`LAB_WORKSPACE`) і реальні model routes
   (`LAB_ROLE_EXECUTOR_URL`, `LAB_ROLE_EXECUTOR_ENABLED=true`).
2. Collector запускає оркестратор у цій копії: static checks → реальний виклик
   роль-екзекутора → запис результатів і audit trail; фіксує, що робоча копія
   не комітилась і нічого не публікувалось.
3. QA (окремий крок) виконує тести в тій самій копії; незалежний reviewer
   перевіряє результати. Висновок — у `verification.json` (R6).

### R5 — 24/7 режим

1. R1–R4 мають бути `PASS` — інакше рядок одразу `BLOCKED`.
2. Owner активує policy gate і видає grants; collector перевіряє активацію,
   grants, budgets/pauses, стан production endpoint'а і наявність артефактів
   `r5-schedule.json`, `r5-notifications.json`.

### R6 — повнота доказів

1. Незалежний виконавець кладе `verification.json` у каталог доказів:
   ```json
   {"verifier": "verification-agent", "collector": "qualify-lab",
    "method": "independent re-check of the hashed artifacts",
    "at": "2026-10-21T00:00:00+00:00"}
   ```
2. Collector перевіряє решту рядків на повноту і звіряє хеші артефактів.

---

## 4. Формат доказу

Кожен рядок (`row`) містить:

| Поле | Значення |
| --- | --- |
| `criterion` | `R1`…`R6` |
| `status` | `PASS` / `FAIL` / `BLOCKED` / `SKIPPED` / `MOCK` |
| `expected` | канонічний текст очікування з каталогу (`lab/qualification.py`) |
| `actual` | що фактично виміряно |
| `started_at`, `finished_at` | ISO-8601 UTC |
| `facts` | `version`, `git_sha` (повний 40-hex), `image_digests`, `environment`, `audit`, `metrics` + специфічні для рядка факти |
| `artifacts` | список `{path, sha256, bytes}` |
| `verification` | `verifier`, `collector`, `method` |

Валідатор (`lab/qualification.py`) відхиляє рядок, якщо:

- відсутній будь-який обов'язковий факт;
- `git_sha` не повний, `version` не `A.B.C.D`, артефакт без `sha256`;
- `expected` не збігається з каталогом;
- `verifier` == `collector` або верифікації немає;
- у доказі є значення, схоже на секрет;
- статус `SKIPPED`/`MOCK` (ніколи не доказ).

Загальний вердикт: `PASS` лише коли **всі** рядки `PASS` і немає проблем;
`FAIL` — є провалений рядок; `BLOCKED` — є заблокований рядок; `INCOMPLETE` —
є пропущений рядок, дублікат або неповний доказ.

---

## 5. Коди виходу

| Код | Значення |
| --- | --- |
| 0 | матриця доведена (`PASS`) |
| 1 | доведено недостатньо (partial/blocked/failed) або режим плану |
| 2 | harness не може виконатися (документ не читається тощо) |

---

## 6. Заборони

- Підготовка цієї процедури не дає права на deploy, GPU-операції,
  production-priority, 24/7 activation, destructive writes чи публікацію.
- Collector не виконує записів у production, не торкається GitHub і не публікує
  код; він лише читає факти, хешує артефакти та перевіряє докази.
- Рядок, який не може бути виміряний, не перетворюється на `PASS` жодним
  «підтвердженням» оператора — лише на `BLOCKED` із конкретною причиною.
