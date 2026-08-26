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
| Нові Job не губляться | Є базово | Під час maintenance/update нові Job переходять у `WAITING_FOR_SYSTEM`, а Watchdog атомарно повертає їх у `NEW` після `NORMAL`. Ще потрібні PostgreSQL/reboot integration tests. |
| Drain активних Job/Worker | Частково | Readiness рахує jobs, busy workers та inflight. Немає distributed barrier, fencing token і захисту від Worker, що перестав відповідати під час drain. |
| Повний backup | Частково | Є app/jobs/PostgreSQL/config backup. Немає формального inventory licenses, encrypted secrets, external object storage, Redis state та перевірки відновлюваності backup. |
| Immutable container update | Є базово | Package розгортається в `releases/<version>`, activation/rollback перемикають `current`, retention захищає active/previous releases; потрібні Docker/power-loss integration tests. |
| DB migrations | Частково | Manifest декларує schema/strategy/rollback safety, а rolling mode забороняє contract migration; ще потрібні backfill orchestration і багаторелізні compatibility tests. |
| Core/Worker health checks | Частково | Role self-tests тепер виконують GPU demo/workflow, Ollama inference, runtime HTTP checks та backup read/write; ще потрібні end-to-end API/Redis/publisher/artifact assertions і production image tests. |
| Worker self-test | Частково | Є періодичний role-specific protocol і dispatch gate. Потрібні реальні model fixtures, attestation persistence та failure/recovery integration tests на кожній production role. |
| Автоматичний rollback | Частково | Application rollback атомарно повертає immutable release, перевіряє backup checksums і автоматично відновлює PostgreSQL dump; compatibility рішення та повний health check старої версії ще потребують integration qualification. |
| Recovery після power loss | Частково | Agent вміє побачити перервану фазу при старті, але немає гарантованого boot trigger для recovery та fault-injection тестів кожної фази. |
| Окремий update journal | Є для прототипу | Потрібні append-only/audit semantics, rotation, export у monitoring та correlation ID на всіх вузлах. |
| Rolling update Worker-вузлів | Частково | Є durable coordinator з drain, `maxUnavailable=1`, target-version pinning, self-test gate і stop-on-first-failure; потрібні multi-host/host-executor integration та canary rollback tests. |
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
| Internal secrets UI | Частково | Додано write-only Web/API керування SMTP/Telegram/YouTube/Facebook/TikTok/SSH/license/AI secrets з AES-GCM persistence; потрібні OAuth-specific flows і key sealing. |
| Zero Shell operations | Частково | Fleet controls, updates та write-only integration secrets доступні у Web UI; ще потрібні users/MFA, models, backup restore, ACME і повна health remediation. |
| Immutable runtime | Частково | Role plan та versioned images є; update path і локальні bind-mounted runtime/config файли ще дозволяють mutable overlay behavior. |

## 3. Deployment Wizard та Node Roles

| Вимога | Стан | Що залишилось |
|---|---|---|
| Розширюваний список ролей | Частково | JSON catalog розширюваний, але нова роль все одно потребує реального Compose service/image та role-specific agent implementation. |
| Встановлювати лише потрібні компоненти | Є на service allowlist рівні | Додати image/package allowlist tests на реальній VM; зараз modules у catalog не доводять, що відповідний runtime існує. |
| Core Node stack | Частково | Основні Core/DB/Redis/UI/Monitoring є. License Manager не реалізований окремо; Publisher/Dispatcher/Scheduler не ізольовані services. |
| GPU Node | Частково | Role plan містить ComfyUI та GPU workflow self-test. Bootstrap ще не завершує NVIDIA Container Toolkit/CUDA compatibility flow і немає physical-GPU release test. |
| Text Node | Частково | Додано окремий Ollama executor і UTF-8 artifact contract; model pull/cache, streaming та lifecycle ще потрібні. |
| Voice Node | Частково | Додано TTS synthesis executor і перевірюваний audio artifact contract; voice model management ще не завершений. |
| Publisher Node | Частково | Додано publisher executor із обов'язковим publication receipt; credential UI, OAuth і platform-specific idempotency ще потрібні. |
| Monitoring Node | Частково | Prometheus і Grafana заявлені з health checks; немає log store/collector, provisioned dashboards і alert rules. |
| Backup Node | Частково | Backup service contract і storage read/write self-test є; snapshot/archive executor, remote retention і restore verification не завершені. |
| Add Worker одноразовий token | Є базово | PostgreSQL atomic consume є. Потрібні token naming/audit, cancellation, explicit capability scope UI та rate limiting. |
| Автоматична реєстрація | Є базово | CSR/mTLS/JWT, automatic certificate renewal, credential generation rotation і binding machine API до актуального certificate serial працюють. Потрібні proxy-level CRL/OCSP і retry-safe enrollment state machine. |
| NAT/VPN outbound connection | Є архітектурно | Потрібні integration tests через NAT, proxy, clock skew, TLS renewal і Core failover. |
| Capability-driven dispatch | Частково | Dispatcher використовує persisted `tested_capabilities`, вимагає свіжий role-matched self-test і відхиляє capability поза role allowlist. Ще потрібні model/module attestation, load score, locality та fair scheduling. |
| Worker states | Частково | Є `WorkerState`, legal transitions та admin actions drain/resume/quarantine/unquarantine/self-test; ще потрібен окремий PostgreSQL transition audit. |
| Worker health | Частково | Heartbeat має GPU, available RAM, disk, CPU load і runtime version; ще потрібні Docker/model status, latency та production role qualification. |
| Однаковий Update Agent на ролях | Частково | Worker створює idempotent local signed-update request для coordinated target version; потрібен production host-executor integration на кожній ролі. |
| Core Dashboard fleet view | Частково | Є basic Worker list/token generation. Немає повного registered-node inventory, role/capability filters, cert expiry, update state, drain/revoke/rotate/self-test actions. |

## Критичні технічні дефекти поточної реалізації

1. **Role catalog перебільшує фактичну функціональність.** `modules` містить ComfyUI,
   CUDA, TTS, backup, Grafana та publisher adapters, хоча service allowlist часто запускає
   лише generic `worker`. UI повинен показувати `planned`, `installed`, `healthy` окремо.
2. **Container Update Agent більше не має Docker socket.** Bootstrap checksum-перевіряє та
   встановлює host-side systemd executor/path/timer, а контейнер запускається read-only без
   capabilities. Ще потрібні disposable-host sandbox tests і подальше звуження privileged API.
3. **Secret store завершено частково.** Core secrets мігруються у versioned AES-256-GCM
   envelope та проходять authentication під час читання. Installation data key і bootstrap
   `.env` поки залишаються локальними mode-`0600` файлами; потрібні TPM/KMS/passphrase sealing,
   Docker secrets, rotation і redaction.
4. **Release key lifecycle реалізовано частково.** Update Agent перевіряє threshold-signed offline-root
   metadata, її version/expiry, channel scope, revocation state та digest online release key. Manifest
   також має durable monotonic `release_sequence`. Ще потрібно підключити root metadata до bootstrap
   distribution, визначити ceremony/escrow root-ключів і перевірити rotation end-to-end.
5. **Safe Update correctness посилено, але не завершено.** Локальний crash-safe lease відхиляє другий
   Update Agent, update audit має перевірюваний hash chain, backup отримав checksum manifest, а rollback
   Core автоматично відновлює PostgreSQL після початку update. Для HA ще потрібні PostgreSQL fencing,
   immutable release activation, expand/contract migrations, rolling coordinator і fault injection.
6. **Немає end-to-end production test matrix.** Unit tests не замінюють fresh Ubuntu VM,
   real PostgreSQL concurrency, Docker Compose, Nginx mTLS і physical GPU tests.

## Рекомендований порядок наступних робіт

### Етап 1 — чесний runtime і self-tests

1. Створити реальні images/services для `comfyui`, `text-worker`, `tts-worker`,
   `publisher-worker`, `backup-service`, `grafana` і log store.
2. Замінити generic Worker role mapping на окремі executors.
3. Додати role-specific self-test protocol та зберігати результат у Node Registry.
4. Дозволяти dispatch лише після `READY + valid heartbeat + successful self-test`.

### Етап 2 — secrets і privileged operations

1. Завершити secret lifecycle: sealing data key, Docker secrets, rotation і redaction
   (AEAD та versioned envelope вже реалізовані).
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
