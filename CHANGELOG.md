# Changelog

## Unreleased

- Виправлено пошкоджене злиття, яке дублювало перевірки безпеки, логіку оновлення, кроки bootstrap, конфігурацію розгортання, секції Web UI та тести; репозиторій знову компілюється, а посилений захист гілки збережено.
- Узгоджено `VERSION`, записи журналу змін і примітки до релізів з опублікованими комітами `0.0.0.3` та `0.0.0.5`.
- Нумерація релізів тепер враховує метадані релізів без тегів, тому наступний номер залишається монотонним; для локальної розробки й тестів додано сумісну з Windows оренду блокування оновлення.
- Стабілізовано тестовий baseline: ізольовано стан API та черги між тестами, актуалізовано heartbeat-контракт Worker, виправлено фонову гонку й додано коректну поведінку Linux-only перевірок у Windows.

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
