# Аналіз прогалин трьох технічних завдань Vertep

Дата перевірки: 2026-08-25

## Загальний висновок

У репозиторії вже є значна частина **control-plane прототипу**: підписані manifests,
maintenance gating, одноразова реєстрація вузлів, PostgreSQL registry, CSR/mTLS,
role catalog, capability filtering, bootstrap і First Run UI. Але жодне з трьох ТЗ ще
не виконано повністю як production appliance. Найбільша розбіжність — декларації ролей
і модулів випереджають реальні runtime services: кілька ролей запускають той самий
універсальний Worker, хоча TTS, backup, publisher, ComfyUI і Grafana services відсутні.

Позначення:

- **Є** — реалізація достатня на рівні поточного ТЗ;
- **Частково** — є API/прототип, але немає повного production lifecycle;
- **Немає** — механізм відсутній або декларація не приводить до потрібної поведінки.

## 1. Safe Update System

| Вимога | Стан | Що залишилось |
|---|---|---|
| Оновлення лише із Vertep Update Server | Частково | Update protocol використовує HTTPS origin і підпис, але bootstrap/update trust key не має формалізованої offline-root rotation/revocation процедури. |
| Автоперевірка, Web UI, CLI | Частково | Усі три entry points існують. Потрібен end-to-end тест systemd timer, authentication і поведінки після reboot. |
| Перевірка версії, підпису, сумісності | Є | Додати key ID, release channel policy, expiry, rollback compatibility та anti-downgrade policy. |
| `MAINTENANCE` і припинення dispatch | Частково | Watchdog і claim блокуються. Немає формальної distributed queue pause/ack між кількома Core replicas. |
| Нові Job не губляться | Частково | Вони залишаються `NEW`; `WAITING_FOR_SYSTEM` оголошено, але фактично не використовується і не тестується lifecycle повернення. |
| Drain активних Job/Worker | Частково | Readiness рахує jobs, busy workers та inflight. Немає distributed barrier, fencing token і захисту від Worker, що перестав відповідати під час drain. |
| Повний backup | Частково | Є app/jobs/PostgreSQL/config backup. Немає формального inventory licenses, encrypted secrets, external object storage, Redis state та перевірки відновлюваності backup. |
| Immutable container update | Частково | Appliance використовує versioned images, але host update path досі копіює release поверх робочого дерева і частково зберігає стару source-oriented логіку. |
| DB migrations | Частково | Є idempotent runner. Немає expand/contract policy, backward-compatible schema window та автоматичного migration rollback/forward recovery. |
| Core/Worker health checks | Частково | Role self-tests тепер виконують GPU demo/workflow, Ollama inference, runtime HTTP checks та backup read/write; ще потрібні end-to-end API/Redis/publisher/artifact assertions і production image tests. |
| Worker self-test | Частково | Є періодичний role-specific protocol і dispatch gate. Потрібні реальні model fixtures, attestation persistence та failure/recovery integration tests на кожній production role. |
| Автоматичний rollback | Частково | Application rollback є, але PostgreSQL dump автоматично не відновлюється, compatibility рішення не формалізоване, health check старої версії неповний. |
| Recovery після power loss | Частково | Agent вміє побачити перервану фазу при старті, але немає гарантованого boot trigger для recovery та fault-injection тестів кожної фази. |
| Окремий update journal | Є для прототипу | Потрібні append-only/audit semantics, rotation, export у monitoring та correlation ID на всіх вузлах. |
| Rolling update Worker-вузлів | Немає | Відсутні порядок, drain одного вузла, self-test, canary, max-unavailable і stop-on-first-failure orchestration. |
| Глобальні `READ_ONLY`/`EMERGENCY` semantics | Частково | Стани оголошені, але більшість API не визначає дозволені операції для кожного стану. |

## 2. Bootstrap Installer та First Run Wizard

| Вимога | Стан | Що залишилось |
|---|---|---|
| Одна команда на Ubuntu 24.04 | Частково | Core має default flow. Non-Core потребує environment variables для role/Core URL/token/CA, тобто це ще не повний zero-shell Web Wizard. |
| CPU/RAM/disk/internet preflight | Є базово | Додати proxy/DNS/NTP/ports checks, supported CPU flags, filesystem type, clock skew і зрозумілий remediation report. |
| NVIDIA/AMD/no-GPU detection | Частково | Detection є. Bootstrap не встановлює NVIDIA Container Toolkit/CUDA runtime повністю; AMD runtime/ROCm не реалізований. |
| Docker/Compose install та autostart | Є базово | Потрібні pinned supported versions, repository signature policy та upgrade compatibility matrix. |
| `/opt/vertep` layout і volumes | Частково | Основні каталоги є. Потрібні ownership per service, quotas, SELinux/AppArmor policy, backup retention і disk-pressure behavior. |
| Signed runtime download | Є базово | Додати offline-root key lifecycle, mirror policy, resumable download та rollback при partial image pull. |
| Усі контейнери `HEALTHY` | Частково | Role services мають health checks і Worker self-test, але потрібна перевірка реальних published images та dependency/failure matrix. |
| HTTPS `:8443` | Є | Сертифікат self-signed. Потрібні SAN для IP/DNS, ACME/custom certificate flow, renewal та browser-friendly onboarding. |
| First Run недоступність системи до завершення | Є базово | Setup code захищає ownership. Потрібні rate limit окремо для setup, expiry setup code і browser E2E tests. |
| Назва та перший admin | Є | Потрібні password policy, recovery codes, MFA/WebAuthn, forced credential rotation і audit event. |
| Автогенерація секретів | Частково | Генерація криптографічна, але secret store — plaintext JSON/`.env`; encryption key генерується, однак не використовується для authenticated encryption. |
| AI backend selection | Частково | Вибір записується, але немає перевірки credentials/connectivity/model, secure API-key entry та runtime reconciliation. |
| Installation Manifest | Частково | Є ID/name/version/hardware/modules. Docker version, фактично запущені images/digests, network, storage, certificate fingerprints і module health не фіксуються повністю. |
| Повний containerized runtime | Немає як заявлено | API/Dispatcher/Scheduler/Publisher досі об'єднані в Core process; License Manager, Backup Service, Grafana, TTS runtime та окремі role engines не реалізовані як заявлені сервіси. |
| Internal secrets UI | Немає | Немає повного Web UI для SMTP/Telegram/YouTube/SSH/license secrets, rotation і encrypted persistence. |
| Zero Shell operations | Немає | Немає завершених Web UI flows для users, licenses, models, backup/restore, certificate renewal, node revoke/rotate, full health remediation. |
| Immutable runtime | Частково | Role plan та versioned images є; update path і локальні bind-mounted runtime/config файли ще дозволяють mutable overlay behavior. |

## 3. Deployment Wizard та Node Roles

| Вимога | Стан | Що залишилось |
|---|---|---|
| Розширюваний список ролей | Частково | JSON catalog розширюваний, але нова роль все одно потребує реального Compose service/image та role-specific agent implementation. |
| Встановлювати лише потрібні компоненти | Є на service allowlist рівні | Додати image/package allowlist tests на реальній VM; зараз modules у catalog не доводять, що відповідний runtime існує. |
| Core Node stack | Частково | Основні Core/DB/Redis/UI/Monitoring є. License Manager не реалізований окремо; Publisher/Dispatcher/Scheduler не ізольовані services. |
| GPU Node | Частково | Role plan містить ComfyUI та GPU workflow self-test. Bootstrap ще не завершує NVIDIA Container Toolkit/CUDA compatibility flow і немає physical-GPU release test. |
| Text Node | Частково | Ollama inference self-test є, але немає окремого text task executor/model lifecycle. |
| Voice Node | Частково | TTS service contract і health self-test є; voice model management та synthesis executor ще не реалізовані в цьому repo. |
| Publisher Node | Частково | Окремий service contract і health self-test є; credential UI, platform connectivity semantics та publisher task executor не завершені. |
| Monitoring Node | Частково | Prometheus і Grafana заявлені з health checks; немає log store/collector, provisioned dashboards і alert rules. |
| Backup Node | Частково | Backup service contract і storage read/write self-test є; snapshot/archive executor, remote retention і restore verification не завершені. |
| Add Worker одноразовий token | Є базово | PostgreSQL atomic consume є. Потрібні token naming/audit, cancellation, explicit capability scope UI та rate limiting. |
| Автоматична реєстрація | Є базово | CSR/mTLS/JWT, automatic certificate renewal і credential generation rotation працюють. Потрібні proxy-level CRL/OCSP і retry-safe enrollment state machine. |
| NAT/VPN outbound connection | Є архітектурно | Потрібні integration tests через NAT, proxy, clock skew, TLS renewal і Core failover. |
| Capability-driven dispatch | Частково | Dispatcher фільтрує capability/VRAM/workflow і в appliance mode вимагає успішний self-test. Немає durable attestation, load score, locality та fair scheduling. |
| Worker states | Частково | `ONLINE`, `FREE`, `READY`, `BUSY`, `UPDATING`, `OFFLINE`, `ERROR` не зведені до одного enum/state machine. |
| Worker health | Частково | Heartbeat має частину GPU metrics. Немає standardized RAM/disk/Docker/version/latency checks та role-specific health contract. |
| Однаковий Update Agent на ролях | Частково | Service є у role plans, але per-node autonomous update, Core notification, coordinated compatibility і rolling policy не завершені. |
| Core Dashboard fleet view | Частково | Є basic Worker list/token generation. Немає повного registered-node inventory, role/capability filters, cert expiry, update state, drain/revoke/rotate/self-test actions. |

## Критичні технічні дефекти поточної реалізації

1. **Role catalog перебільшує фактичну функціональність.** `modules` містить ComfyUI,
   CUDA, TTS, backup, Grafana та publisher adapters, хоча service allowlist часто запускає
   лише generic `worker`. UI повинен показувати `planned`, `installed`, `healthy` окремо.
2. **Update Agent має unrestricted Docker socket.** Компрометація контейнера дорівнює
   root compromise host. Потрібен host-side вузький RPC або socket proxy allowlist.
3. **Secret store не зашифрований.** `ENCRYPTION_KEY` існує, але secrets залишаються
   plaintext. Потрібен AEAD, key sealing, rotation і redaction.
4. **Release key lifecycle не визначено.** Потрібні offline root, online release keys,
   `key_id`, validity, revocation і root-signed rotation metadata.
5. **Немає end-to-end production test matrix.** Unit tests не замінюють fresh Ubuntu VM,
   real PostgreSQL concurrency, Docker Compose, Nginx mTLS і physical GPU tests.

## Рекомендований порядок наступних робіт

### Етап 1 — чесний runtime і self-tests

1. Створити реальні images/services для `comfyui`, `text-worker`, `tts-worker`,
   `publisher-worker`, `backup-service`, `grafana` і log store.
2. Замінити generic Worker role mapping на окремі executors.
3. Додати role-specific self-test protocol та зберігати результат у Node Registry.
4. Дозволяти dispatch лише після `READY + valid heartbeat + successful self-test`.

### Етап 2 — secrets і privileged operations

1. Реалізувати encrypted secret store з AEAD і versioned key envelope.
2. Прибрати Docker socket із контейнера Update Agent.
3. Додати credential/certificate/key rotation та backup redaction.

### Етап 3 — update orchestration

1. Rolling/canary Worker update з `maxUnavailable=1`.
2. Fault-injection tests для кожної durable update phase.
3. Перевірений PostgreSQL restore та backward-compatible migration policy.
4. Fleet-wide version compatibility й stop-on-first-failure.

### Етап 4 — Zero Shell UI

1. Users/MFA, licenses, models, integrations і encrypted secrets.
2. Backup/restore, retention і restore verification.
3. Fleet inventory, drain/revoke/rotate/update/self-test controls.
4. Certificate/ACME management, diagnostics і remediation guidance.

## Мінімальні release gates

- Кожна роль на чистій Ubuntu VM запускає лише дозволені images і проходить свій self-test.
- GPU test виконує реальний minimal ComfyUI workflow на підтримуваній NVIDIA GPU.
- Voice/Text/Publisher/Backup tests перевіряють реальний executor, а не import process.
- Два Core replicas не можуть використати один registration token двічі.
- Revoked/expired certificate і JWT не проходять machine API.
- Update fault injection у кожній фазі завершується old-good або new-good runtime.
- Restore test відновлює PostgreSQL, configs, secrets і artifacts на чистому host.
- Жоден secret не потрапляє у logs, API response, Compose output, process args або backup metadata.
- Усі штатні операції виконуються через Web UI без shell.
