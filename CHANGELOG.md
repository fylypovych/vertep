# Changelog

## Unreleased

## 0.0.0.64 - 2026-09-01

- Made emergency mode actionable: the Errors tab now shows the durable system reason, failed update/deployment history and technical details, while administrators can return to normal mode only after CORE, PostgreSQL and Redis pass health checks.
- Moved local Core-role activation into Nodes, separated optional services from the base Core plan, fixed backup-service access to protected appliance data, and made signed role-catalog updates reach existing installations.
- Replaced workflow parameter JSON blocks with typed Ukrainian fields and added dependency-aware workflow deletion; replaced the brand JSON editor with a guided form and added brand deletion.
- Moved new-job creation from Overview to Tasks and added `vertep start`, `vertep stop`, `vertep restart`, and `vertep recover` operations.
- Prevented normal application updates and restarts from recreating PostgreSQL or Redis, avoiding false emergency transitions caused by terminated state-store connections.

## 0.0.0.62 - 2026-09-01

- Added a privileged server-restart action to the administration panel and made updates automatically recreate every active Core-role service while leaving PostgreSQL and Redis running.
- Added a Ukrainian five-stage update view with live percentage progress, readable current-step messages, temporary restart handling, and automatic page refresh after success.
- Preserved detailed progress emitted by the host update workflow in the final update history and added API, executor, contract, and browser coverage for restart operations.

## 0.0.0.60 - 2026-09-01

- Replaced raw queue and system JSON with Ukrainian status cards, readable update history, and a consolidated maintenance view for backups, models, and TLS certificates.
- Replaced the full-document workflow JSON editor with a guided Ukrainian node editor that validates node identifiers, action types, and per-node parameters.
- Added administrator-controlled checkboxes for activating multiple local worker roles on a Core host; signed composite deployment plans add or remove only the required services while preserving data and limiting the local worker to selected capabilities.
- Made status recovery tolerate legacy worker records without heartbeat timestamps instead of failing the entire dashboard.

## 0.0.0.58 - 2026-09-01

- Fixed admin-panel updates stopping before installation with `Connection refused`: the privileged host updater now drains through the appliance's exposed HTTPS proxy on port 8443 instead of the unexposed container-only Core port 8080, while remote Core URLs retain normal TLS verification.

## 0.0.0.56 - 2026-09-01

- Completed the Ukrainian localization of the node onboarding wizard, including its title, role names and explanatory terminology; browser coverage now protects every role label from English-language regressions.

## 0.0.0.54 - 2026-09-01

- Replaced the raw character JSON editor with a responsive Ukrainian form for identity, language, behavior, appearance, voice, generation and publishing settings; editing now loads the current character and opens reliably.
- Localized the dashboard navigation, actions, statuses and operator-facing lifecycle terminology in Ukrainian, while preserving technical identifiers where they are needed for configuration.
- Added JavaScript contract coverage and browser tests for both creating and editing characters without console errors.

## 0.0.0.52 - 2026-09-01

- Fixed the dashboard's fatal inline JavaScript syntax error, restoring navigation, health refresh and lifecycle controls; CI now parses every inline dashboard script with Node.js.
- Bootstrap installs the host update executor's PostgreSQL driver and safely supersedes any queued request when an explicit bootstrap run takes over, preventing `No module named psycopg` and stale `PENDING` state.
- `vertep status` now falls back to the managed environment for the node role/name, prints the installed version and exposes update progress instead of reporting `VERTEP UNKNOWN`.
- Browser smoke tests now assert a working dashboard load, error-free JavaScript navigation and the public health contract.

## 0.0.0.49 - 2026-09-01

- `vertep update` and the periodic update checker now authenticate with the host-only internal key instead of obsolete bootstrap administrator credentials, so First Run account creation no longer causes HTTP 401 responses.
- Bootstrap, update and rollback paths remove only Docker Compose's stale hash-prefixed replacement containers before recreation, allowing interrupted updates to resume without container-name conflicts while preserving volumes and configuration.
- The privileged update agent uses the same internal key for drain-readiness checks during an update.
- Host-side `vertep status` now uses internal authentication as well, and signed runtime contracts report database schema generation 9 after the node-token expansion migration.

## 0.0.0.47 - 2026-09-01

- Resumed installations now resolve services and capabilities from the downloaded role catalog using its actual top-level schema; repeating bootstrap after First Run no longer fails with `Cannot iterate over null`.
- Bootstrap rejects a persisted role that is absent from the signed catalog with an explicit diagnostic and no longer offers the unsupported legacy `core-worker` role.

## 0.0.0.45 - 2026-08-31

- Added a forward-only database migration for the node registration token `push_token` flag, fixing the First Run completion error on both existing and fresh installations.
- Core registration tokens and integration secrets are now prepared before First Run is irreversibly marked complete, preventing a post-commit failure from stranding the wizard in a partially completed state.

## 0.0.0.43 - 2026-08-31

- First Run now defers validation and model installation for the appliance-managed Ollama backend until role deployment has started Ollama; Core and Text roles wait for the service and then pull the selected model.
- The wizard completes setup before opening the Installation Manifest step, fills the manifest before enabling its download button, and no longer claims an unfinished manifest is already available.

## 0.0.0.41 - 2026-08-31

- First Run Wizard no longer crashes after a successful setup API response: the missing Core connection controls are present, role rendering tolerates absent optional markup, and manual setup-code entry updates the URL instead of reloading the stale token forever.

## 0.0.0.39 - 2026-08-31

- `/setup` now redirects to the actual First Run Wizard at `/setup.html` while preserving the one-time token query parameter, instead of returning the unconfigured-runtime placeholder.

## 0.0.0.37 - 2026-08-31

- Proxy healthcheck now targets IPv4 loopback explicitly, avoiding Alpine resolving `localhost` to an unbound IPv6 address and reporting a healthy nginx process as `unhealthy`.

## 0.0.0.35 - 2026-08-31

- Compose now passes the configured `WEB_DOMAIN` into the proxy container, allowing the standalone entrypoint to render a valid nginx `server_name`.
- Bootstrap reports container state every 30 seconds while waiting for runtime health instead of appearing idle until the timeout.

## 0.0.0.33 - 2026-08-31

- Proxy startup logic moved from a quote-sensitive inline Compose command to a standalone image entrypoint; fresh and resumed installations no longer enter a restart loop with `unexpected end of file`.
- The proxy image now renders the configured `WEB_DOMAIN` from its template before starting nginx and continues to reload nginx when the TLS certificate or node CRL changes.

## 0.0.0.31 - 2026-08-31

- Bootstrap більше не викликає `docker compose wait` для вже завершеного one-shot контейнера `migrate`; код завершення міграції перевіряється безпосередньо через Docker inspect, тому успішний resume доходить до healthcheck і показу Setup URL.
- Startup Recovery unit отримав коректну секцію `[Install]`, тож `systemctl enable` більше не виводить попередження про static unit.

## 0.0.0.29 - 2026-08-31

- Bootstrap став resumable та idempotent: повторний запуск зберігає паролі PostgreSQL/Redis, ключі, TLS/Node CA, роль, домен, довільні локальні параметри й Docker volumes, оновлюючи лише підписаний runtime та керовані release-параметри.
- Resume відмовляється генерувати нові credentials поверх наявних Docker volumes або encrypted secret store, якщо відповідний ключ втрачено, замість створення несумісного частково працездатного стану.
- Self-update підключено безпосередньо до GitHub Releases: release workflow публікує окремий підписаний update manifest, updater перевіряє підпис і SHA-256 пакета, а download allowlist обмежено репозиторієм `fylypovych/vertep`.
- Після успішного оновлення host-side executors і systemd units синхронізуються з активним підписаним release; CLI коректно визначає роль із `NODE_ROLE` і працює через локальний HTTPS proxy.

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
