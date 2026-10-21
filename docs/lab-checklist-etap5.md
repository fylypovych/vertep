# Чекліст Етапу 5 — Docker-розгортання та Systemd

**Мета:** запустити лабораторію в ізольованому Docker-середовищі на окремій Ubuntu VM.

**Попередні умови:**
- [ ] Ubuntu 24.04 LTS VM готова
- [ ] Docker та Docker Compose встановлені
- [ ] Git клонований або runtime bundle розпакований
- [ ] Python 3.10+ доступний

---

## 1. Підготовка VM

### 1.1 Перевірити ресурси

```bash
# CPU
nproc
# Очікується: 4-8 vCPU

# RAM
free -h
# Очікується: 16-32 ГБ RAM

# Disk
df -h
# Очікується: 100-200 ГБ SSD
```

### 1.2 Встановити Docker

```bash
# Ubuntu 24.04
sudo apt update
sudo apt install -y docker.io docker-compose-plugin

# Додати користувача в docker group
sudo usermod -aG docker $USER
newgrp docker

# Перевірити
docker --version
docker compose version
```

### 1.3 Створити директорії

```bash
sudo mkdir -p /opt/vertep-lab
sudo chown $USER:$USER /opt/vertep-lab

cd /opt/vertep-lab
```

### 1.4 Скопіювати файли

```bash
# Якщо з Git clone
cp -r /path/to/vertep/deploy/lab /opt/vertep-lab/
cp -r /path/to/vertep/lab /opt/vertep-lab/
cp -r /path/to/vertep/installer /opt/vertep-lab/
cp /path/to/vertep/requirements.txt /opt/vertep-lab/

# Якщо з runtime bundle
tar -xzf vertep-runtime-*.tar.gz
cp -r vertep/deploy/lab /opt/vertep-lab/
cp -r vertep/lab /opt/vertep-lab/
cp -r vertep/installer /opt/vertep-lab/
```

---

## 2. Налаштування конфігурації

### 2.1 Створити lab.env

```bash
cat > /opt/vertep-lab/lab.env << 'EOF'
# GPU Lease (Етап 4)
LAB_VERTEP_URL=https://vertep-core.example.com/lab-lease/v1
LAB_VERTEP_TOKEN=...
LAB_IDLE_SHARING_ENABLED=false

# Policy Gate
LAB_POLICY_GATE_ACTIVE=false

# State directory
LAB_STATE_DIR=/var/lib/vertep-lab

# Workspace
LAB_WORKSPACE=/srv/vertep-lab/repo

# Repository
LAB_REPOSITORY_URL=https://github.com/fylypovych/vertep.git
LAB_BRANCH=main

# Actor
LAB_ACTOR=lab

# Owner (секрет власника: grant/revoke вимагають його пред'явлення)
LAB_OWNER_TOKEN=<секрет-власника>
LAB_OWNER_IDENTITY=owner

# GitHub transport (без токена й репозиторію транспорт лишається dry-run)
LAB_GITHUB_TOKEN=
LAB_GITHUB_REPOSITORY=owner/repo
LAB_GITHUB_API_URL=
EOF
```

### 2.2 Встановити права

```bash
sudo chmod 600 /opt/vertep-lab/lab.env
```

### 2.3 Налаштувати GPU (якщо Етап 4 виконано)

```bash
# Увімкнути idle sharing тільки після успішного Етапу 4
sudo sed -i 's/LAB_IDLE_SHARING_ENABLED=false/LAB_IDLE_SHARING_ENABLED=true/' /opt/vertep-lab/lab.env
```

---

## 3. Docker Compose

### 3.1 Перевірити docker-compose.yml

```bash
cd /opt/vertep-lab
cat deploy/lab/docker-compose.yml
```

Перевірити:
- [ ] Відсутність mount production paths (`/opt/vertep`, `/var/run/docker.sock`)
- [ ] `lab-repo` volume в режимі `ro` (read-only)
- [ ] `lab-state` volume для персистентності
- [ ] Health check присутній
- [ ] Ресурси обмежені (cpus, memory)

### 3.2 Перевірити Dockerfile

```bash
cat deploy/lab/Dockerfile
```

Перевірити:
- [ ] Базовий образ: `python:3.10-slim`
- [ ] Відсутність production credentials
- [ ] Копіювання лише `lab/` та необхідних файлів
- [ ] CMD: `python -m lab.cli status`

### 3.3 Перевірити Dockerfile.sandbox

```bash
cat deploy/lab/Dockerfile.sandbox
```

Перевірити:
- [ ] `cap_drop: ALL`
- [ ] `cap_add: NET_BIND_SERVICE`
- [ ] `read_only: true`
- [ ] `tmpfs: /tmp`
- [ ] `security_opt: no-new-privileges:true`

---

## 4. Запуск контейнерів

### 4.1 Побудувати образи

```bash
cd /opt/vertep-lab
docker compose -f deploy/lab/docker-compose.yml build
```

**Очікуваний результат:**
- `lab-control` image успішно побудовано
- `lab-sandbox` image успішно побудовано

### 4.2 Запустити stack

```bash
docker compose -f deploy/lab/docker-compose.yml up -d
```

**Очікуваний результат:**
- `lab-control` запущено
- `lab-sandbox` не запущено (run-once)

### 4.3 Перевірити статус

```bash
docker compose -f deploy/lab/docker-compose.yml ps
```

**Очікуваний результат:**
- `lab-control` — `Up` або `healthy`
- Відсутність errors

### 4.4 Перевірити логи

```bash
docker compose -f deploy/lab/docker-compose.yml logs lab-control
```

**Очікуваний результат:**
- Відсутність помилок
- Policy gate ініціалізовано
- State directory створено

---

## 5. Перевірка CLI

### 5.1 Виконати CLI в контейнері

```bash
docker compose -f deploy/lab/docker-compose.yml exec lab-control python -m lab.cli status
```

**Очікуваний результат:**
```json
{
  "workspace": {...},
  "policy": {
    "activated": false,
    "active_grants": [],
    ...
  },
  "budget": {...},
  "roles": [...],
  "queue": [],
  "handoff": {}
}
```

### 5.2 Перевірити audit

```bash
docker compose -f deploy/lab/docker-compose.yml exec lab-control python -m lab.cli audit
```

**Очікуваний результат:**
```json
{
  "records": 0,
  "valid": true,
  "problems": [],
  "path": "/var/lib/vertep-lab/audit.jsonl"
}
```

### 5.3 Перевірити check

```bash
docker compose -f deploy/lab/docker-compose.yml exec lab-control python -m lab.cli check local_fix
```

**Очікуваний результат:**
```json
{
  "allowed": true,
  "action": "local_fix",
  "actor": "lab",
  "subject": "",
  "rule": "standing_permission",
  "reason": "allowed inside the isolated laboratory copy"
}
```

---

## 6. Мережева ізоляція

### 6.1 Перевірити мережу

```bash
docker network inspect vertep-lab_lab-net
```

**Очікуваний результат:**
- Network driver: `bridge`
- Відсутність external connections до production

### 6.2 Перевірити доступ до endpoints

```bash
docker compose -f deploy/lab/docker-compose.yml exec lab-control curl -I https://github.com
```

**Очікуваний результат:**
- GitHub доступний (для git clone)
- Production не доступний без токена

### 6.3 Перевірити відсутність production access

```bash
# Спроба підключитися до production Docker socket
docker compose -f deploy/lab/docker-compose.yml exec lab-control ls /var/run/docker.sock
```

**Очікуваний результат:**
- `No such file or directory` (socket не mounted)

```bash
# Спроба доступу до production volumes
docker compose -f deploy/lab/docker-compose.yml exec lab-control ls /opt/vertep
```

**Очікуваний результат:**
- `No such file or directory` (production paths не mounted)

---

## 7. Systemd

### 7.1 Скопіювати systemd units

```bash
sudo cp /opt/vertep-lab/installer/vertep-lab.service /etc/systemd/system/
sudo cp /opt/vertep-lab/installer/vertep-lab.timer /etc/systemd/system/
```

### 7.2 Перевірити service

```bash
cat /etc/systemd/system/vertep-lab.service
```

Перевірити:
- [ ] `WorkingDirectory=/opt/vertep-lab`
- [ ] `EnvironmentFile=-/opt/vertep-lab/lab.env`
- [ ] `ExecStart=/usr/bin/docker compose -f /opt/vertep-lab/docker-compose.yml up lab-control`
- [ ] `Restart=on-failure`

### 7.3 Перевірити timer

```bash
cat /etc/systemd/system/vertep-lab.timer
```

Перевірити:
- [ ] `OnCalendar=*:0/15` (кожні 15 хвилин)
- [ ] `RandomizedDelaySec=120`
- [ ] `Persistent=true`

### 7.4 Активувати systemd

```bash
sudo systemctl daemon-reload
sudo systemctl enable vertep-lab.timer
sudo systemctl start vertep-lab.timer
```

### 7.5 Перевірити статус

```bash
sudo systemctl status vertep-lab.timer
sudo systemctl status vertep-lab.service
```

**Очікуваний результат:**
- `vertep-lab.timer` — `active (waiting)`
- `vertep-lab.service` — `active (running)` або `inactive` (залежно від schedule)

---

## 8. Моніторинг

### 8.1 Перевірити логи systemd

```bash
sudo journalctl -u vertep-lab.service -f
```

### 8.2 Перевірити docker logs

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml logs -f lab-control
```

### 8.3 Перевірити state directory

```bash
ls -la /var/lib/vertep-lab/
```

**Очікуваний результат:**
- `audit.jsonl` — audit trail
- `budget.json` — бюджет
- `grants.json` — grants
- `queue.json` — черга задач
- `handoff.json` — handoff між запусками

---

## 9. GPU перевірка (опціонально, після Етапу 4)

### 9.1 Перевірити відсутність GPU passthrough

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control nvidia-smi
```

**Очікуваний результат:**
- `command not found` або `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`
- GPU НЕ передається в контейнер

### 9.2 Перевірити мережевий доступ до GPU endpoint

```bash
# Якщо LAB_VERTEP_URL налаштовано
docker compose -f /opt/vertep-lab/docker-compose.yml exec lab-control curl -H "X-Vertep-Lab-Token: $LAB_VERTEP_TOKEN" $LAB_VERTEP_URL/status
```

**Очікуваний результат:**
- JSON response з contract_version
- GPU доступний тільки через мережу

---

## 10. Критерії приймання

- [ ] 5.1: `docker compose up` без помилок
- [ ] 5.2: `lab.cli status` працює в контейнері
- [ ] 5.3: Мережева ізоляція: доступ лише до дозволених endpoints
- [ ] 5.4: Ресурси VM адекватні (4–8 vCPU, 16–32 ГБ RAM, 100–200 ГБ SSD)
- [ ] 5.5: GPU НЕ передається через passthrough
- [ ] 5.6: GPU доступний тільки через мережевий endpoint (якщо Етап 4 виконано)
- [ ] 5.7: Systemd timer активний та працює
- [ ] 5.8: Лабораторія ізольована від production (no Docker socket, no volumes, no secrets)
- [ ] 5.9: State directory персистентний після рестарту
- [ ] 5.10: Audit trail працює

---

## 11. Зупинка та очищення

### 11.1 Зупинити stack

```bash
docker compose -f /opt/vertep-lab/docker-compose.yml down
```

### 11.2 Зупинити systemd

```bash
sudo systemctl stop vertep-lab.timer
sudo systemctl disable vertep-lab.timer
sudo systemctl stop vertep-lab.service
```

### 11.3 Видалити volumes

```bash
docker volume rm vertep-lab_lab-state vertep-lab_lab-repo
```

### 11.4 Видалити файли

```bash
sudo rm -rf /opt/vertep-lab
sudo rm /etc/systemd/system/vertep-lab.service
sudo rm /etc/systemd/system/vertep-lab.timer
sudo systemctl daemon-reload
```
