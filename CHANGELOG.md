# Changelog

## Unreleased

## 0.0.0.27 - 2026-08-31

- License Manager healthcheck більше не намагається ініціалізувати зашифроване сховище у read-only каталозі під час чистої інсталяції.
- Startup Recovery вмикається для наступних завантажень системи, але більше не запускається паралельно з початковим Compose deployment.

## 0.0.0.25 - 2026-08-31

- Виправлено перший запуск PostgreSQL: випадкові паролі тепер URL-безпечні, а Core і `migrate` використовують libpq DSN, тому символи Base64 більше не пошкоджують hostname або порт у `DATABASE_URL`.

## 0.0.0.23 - 2026-08-31

- Публічне розгортання об’єднано в одному репозиторії `fylypovych/vertep`: Bootstrap отримує підписаний runtime із GitHub Releases, а digest-pinned образи — з GHCR; додано автоматичне підписування, публікацію та перевірку bundle.
- Правило послідовних назв комітів і релізів підтримує перенесення за основою 100: після `0.0.0.99` — `0.0.1.0`, після `0.0.99.99` — `0.1.0.0`.
- Тести ізольовано у тимчасових каталогах і синхронізовано з фоновими задачами, щоб Linux CI не звертався до `/data` та не успадковував незавершені задачі сусідніх тестів.
- Публікація основного інсталяційного релізу більше не блокується надвеликим експериментальним AMD/ROCm образом, який перевищує ліміт GitHub-hosted runner; підтримувані образи публікуються атомарно перед створенням Release.
- Вилучено зайву зміну видимості GHCR через несумісний API: образи, опубліковані з публічного репозиторію, перевіряються анонімним registry-запитом і не блокують створення GitHub Release.
- Додано PostgreSQL-backed координацію rolling update для всього кластера: глобальний fencing через advisory lock та epoch, детермінований порядок вузлів, явне просування canary, блокування dispatch під час update/rollback, автоматичний і ручний rollback до попередньої версії та відновлення після перерваного запуску.
- Міграції підтримують resumable backfill-модулі з durable checkpoint; backup/restore тепер охоплює конфігурацію, storage, PostgreSQL і Redis, перевіряє SHA-256 та безпечно відновлює AES-256-GCM snapshot без path traversal.
- Fleet readiness тепер формується з persisted Jobs і Workers та вимагає завершення drain усіма зареєстрованими вузлами; Worker обробляє окремі update і rollback requests та не приймає нові задачі під час зміни версії.
- Production Bootstrap встановлює й перевіряє NVIDIA Container Toolkit або AMD ROCm, використовує окремі signed Compose overlays для NVIDIA/AMD та зберігає правильний GPU runtime під час deployment, update, rollback, watchdog і startup recovery.
- License Manager, Dispatcher, Scheduler і Certificate Manager винесено в окремі health-checked runtime-сервіси; Core використовує окремі Dispatcher/Scheduler boundaries, а License Manager читає write-only license key із зашифрованого secret store.
- First Run перевіряє AI endpoint, HTTPS-вимоги, credentials і наявність вибраної моделі; локальну Ollama-модель за потреби можна встановити автоматично без shell-доступу.
- Installation Manifest доповнено фактичними Docker image ID/digest, станом контейнерів і health установлених модулів; deployment завершується лише після переходу всіх вибраних сервісів у healthy state.
- У Web UI додано Zero-Shell lifecycle для створення та відновлення backup, встановлення й видалення Ollama models, перегляду та оновлення TLS certificate, а також API для отримання актуального Installation Manifest.
- Runtime-конфігурацію Proxy, Prometheus, Loki, Promtail і Grafana вбудовано в незмінні образи; mutable bind-mounted configuration overlays вилучено з production Compose.
- Виправлено пошкоджене злиття, яке дублювало перевірки безпеки, логіку оновлення, кроки bootstrap, конфігурацію розгортання, секції Web UI та тести; репозиторій знову компілюється, а посилений захист гілки збережено.
- Узгоджено `VERSION`, записи журналу змін і примітки до релізів з опублікованими комітами `0.0.0.3` та `0.0.0.5`.
- Нумерація релізів тепер враховує метадані релізів без тегів, тому наступний номер залишається монотонним; для локальної розробки й тестів додано сумісну з Windows оренду блокування оновлення.
- Стабілізовано тестовий baseline: ізольовано стан API та черги між тестами, актуалізовано heartbeat-контракт Worker, виправлено фонову гонку й додано коректну поведінку Linux-only перевірок у Windows.
- Додано підписаний контракт runtime-релізу версії 2, який зв’язує каталог ролей, файловий inventory, CycloneDX SBOM, сумісність API/бази даних і незмінні digest контейнерних образів; Bootstrap тепер передає Compose лише перевірені образи.
- Update Agent тепер записує глобальний стан у переданий йому каталог операції, не покладаючись на неявний системний шлях; це усуває розбіжність між журналом агента та станом Core у тестах і нестандартних інсталяціях.
- Linux CI тепер перевіряє appliance Compose з тим самим розташуванням `.env`, яке використовує встановлений runtime.
- Додано окремі TTS, Publisher і Backup runtime-сервіси та непривілейовані контейнерні образи: TTS повертає справжній WAV через `espeak-ng`, Publisher забезпечує ідемпотентні receipts без удаваного live-успіху, а Backup створює AES-256-GCM-зашифровані snapshot із SHA-256.
- Monitoring Node отримав Prometheus rules, Loki, Promtail, захищену Grafana з автоматично налаштованими джерелами даних і початковим dashboard журналів та стану runtime.
- Bootstrap став незалежним від майбутньої ролі вузла: він запускає лише тимчасовий контур налаштування, Web Wizard формує перевірюваний запит, а привілейований host-executor застосовує виключно сервіси з підписаного каталогу ролей і прибирає тимчасовий Core для non-Core вузлів.

## 0.0.0.5 - 2026-08-28

- Added coordinated rolling updates with maintenance leases, workload draining, durable phases, health checks, and automatic rollback.
- Added threshold-signed root metadata, release-key authorization, replay protection, and node-certificate revocation enforcement at the proxy.
- Added immutable release preparation, atomic activation, rollback, and retention tooling.
- Added encrypted write-only integration secrets and tightened secret-update authorization and role isolation.
- Added capability-scoped worker role execution and expanded node enrollment, certificate rotation, and self-test behavior.
- Added reproducible appliance qualification gates and CI evidence generation.
- Hardened bootstrap host validation, runtime dependencies, updater service isolation, and recovery after interrupted updates.

## 0.0.0.3 - 2026-08-25

- Added the production appliance bootstrap flow and the seven-step First Run Wizard.
- Added signed, checksum-verified update packages, periodic release checks, backups, health checks, and rollback support.
- Added the node registry, one-use worker enrollment tokens, node-bound credentials, certificate attestation, and capability-driven dispatch.
- Added extensible node roles and deployment planning from `config/node_roles.json`.
- Added the production proxy and Compose topology with persistent services and TLS termination.
- Added database migrations for the node registry and certificate lifecycle.
- Added Worker hardware self-tests and automatic rotation of expiring node certificates.
- Added deployment security and requirements-gap documentation with integration coverage.

## 0.0.0.1 - 2026-08-22

- Added stage/scene DAG state, attempt history and per-scene fan-out/fan-in dispatch.
- Added per-scene retries, dead-letter queue, delayed Job scheduling and worker task recovery.
- Added SHA-256 artifact manifests, provenance, integrity checks and verified downloads.
- Added dependency-free raw uploads plus portable ZIP project import/export.
- Added per-scene TTS, exact scene timing and concatenated narration.
- Added normalized PostgreSQL scene, artifact and stage-attempt persistence.
- Added timeline, uploads, export and dead-letter controls to the Web UI and scene progress to Telegram.
- Added optimistic Job version checks and expanded integration coverage.
- Added crash-safe scene recovery, script/TTS/publisher stage retries and validated platform metadata.
- Added capability-aware dispatch and removed delayed-task head-of-line blocking.
- Rebuilt the Web UI as clean UTF-8 Ukrainian markup with queue, scheduler and orchestration views.
- Added a standalone WORKER Compose stack that cannot start CORE, PostgreSQL or Redis.
- Made update, rollback and status role-aware, including node-token status and offline CORE handling.
- Added manifest-driven installer packages and expanded read-only preflight checks.
- Isolated host ComfyUI on loopback and added lockout-safe SSH key hardening plus default-deny UFW policy.
- Changed CORE updates to wait for PostgreSQL and apply migrations before rebuilding the application.
- Bound every task result to its lease-owning Worker and cancel sibling tasks after fatal scene failure.
- Added binary signature validation, atomic artifact staging and serialized per-Job result processing.
- Made Workers convert rejected generated artifacts into normal failed results for retry/DLQ handling.
- Registered Telegram attachments in the artifact manifest and enforced verified downloads on legacy file routes.
- Added first-class distributed video artifacts and FFmpeg concatenation of scene clips.
- Added an explicit Worker ERROR heartbeat when a required GPU is unavailable.
- Added Web controls for deletion, publication retry, Brands and Workflows.
- Made installer roles/profile composition extensible through the manifest and optional role hooks.
- Added GitHub Actions checks for Python, tests, shell syntax and Compose configurations.
- Added a GTX 1660 6 GB/Turing Worker profile with pinned CUDA 12.4 PyTorch wheels, ComfyUI low-VRAM mode, CUDA verification and heartbeat metadata.
- Added administrator-only GitHub update checks and installation from the Web UI through a persistent host-side systemd agent without exposing the Docker socket.

## Legacy development history

The following headings predate the sequential `0.0.0.N` release series and are retained for historical context; they were not releases in the current version sequence.

### 0.5.0 (pre-release)

- Added atomic Redis Lua claim/requeue, lease renewal and distributed watchdog locking.
- Added CORE-to-WORKER cancellation and ComfyUI interrupt handling.
- Normalized event, task-attempt and Telegram-update persistence.
- Added migration tracking and PostgreSQL-backed Job sequences.
- Added presets, watermarks, ffprobe validation and Telegram attachment downloads.
- Added SSE progress, previews, pagination, Brand UI, cleanup API, CLI and JSON schemas.

### 0.4.0 (pre-release)

- Added repository abstraction and PostgreSQL-ready persistence contracts.
- Added leased priority tasks, watchdog recovery and idempotent results.
- Added multi-scene FFmpeg, mock TTS, audio mixing and subtitle manifests.
- Added Character, Brand and Workflow registries with validation.
- Added structured logs, metrics, alerts and worker log ingestion.
- Added Telegram commands and honest publisher state handling.
- Added role-aware sessions, CSRF, hashed worker tokens and security checks.
- Added installer dry-run, generated secrets, backups and restore tooling.
