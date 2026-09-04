# Vertep

Vertep is a modular content-factory orchestrator for Ubuntu Server 24.04. CORE owns jobs and dispatches GPU work; WORKER runs replaceable ComfyUI workflows and returns artifacts; CORE assembles a valid MP4 with FFmpeg.

## Production installation

On a clean Ubuntu Server 24.04 host, run:

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

The command uses this same public `fylypovych/vertep` repository from start to finish. Bootstrap validates the host, installs Docker and the detected NVIDIA/AMD runtime, downloads the latest signed GitHub Release from this repository, verifies every runtime file, pulls digest-pinned images from this repository's public GHCR packages, generates credentials and TLS material, starts the selected services, and waits until they are healthy. No second repository or external release server is required.

If the first installation is interrupted or a container fails its healthcheck, run the same command again. Bootstrap resumes the existing appliance: it reuses PostgreSQL/Redis volumes, passwords, encrypted-store keys, TLS/Node CA, the selected role, domain and local settings, then replaces only release-managed files and signed image references. It refuses to invent replacement credentials when persistent data already exists but its key file is missing. A VM snapshot or Ubuntu reinstall is not part of the normal recovery procedure.

When installation finishes, open the printed `https://SERVER-IP:8443` address and complete the First Run Wizard. Further setup, backups, models, certificates, node enrollment and signed updates are managed through the Web UI without rerunning Bootstrap. Development and advanced source installations are documented under [Legacy/source installation](#legacysource-installation).

## Demo

```bash
cp .env.example .env
# Set unique ADMIN_PASSWORD, NODE_API_TOKEN and POSTGRES_PASSWORD values.
docker compose up --build
```

Open `http://localhost:8080` and sign in as `ADMIN_USER` (default `admin`). Demo mode still crosses the CORE/WORKER task protocol, but uses a deterministic image instead of a GPU model. The resulting `jobs/<job_id>/final/video.mp4` is a real MP4.

For a local CORE-only developer run, set `LOCAL_WORKER_FALLBACK=true`. This fallback exists for development and is disabled in the supplied Compose environment.

## Legacy/source installation

For development or advanced source deployments, run `sudo ./install.sh`, then choose `CORE`, `GPU WORKER`, or both. Production appliances should use the signed [Production installation](#production-installation) flow. The source installer:

- validates Ubuntu 24.04;
- installs Docker, Compose, Git, Python, FFmpeg and firewall rules;
- installs Ollama for CORE;
- installs the recommended NVIDIA driver, NVIDIA Container Toolkit and ComfyUI for WORKER;
- detects model, VRAM, temperature, load and CUDA availability;
- installs the host-side Web update watcher on CORE without exposing the Docker socket;
- writes `/etc/vertep/node.conf` and enables only the applicable systemd units.

Driver installation can require a reboot and a second installer run. Put a GPU-compatible checkpoint in `/opt/ComfyUI/models/checkpoints` and set its filename as `COMFYUI_CHECKPOINT`. The included API-format workflow is usable with standard Stable Diffusion checkpoints and can be replaced per character. For Pascal cards the installer selects a pinned CUDA 12.1/PyTorch line instead of blindly installing the newest build.

### NVIDIA GeForce GTX 1660 (6 GB)

The Worker installer recognizes GTX 1660, GTX 1660 SUPER and GTX 1660 Ti as Turing (compute capability 7.5). The 6 GB profile uses pinned PyTorch 2.6.0 CUDA 12.4 wheels and starts ComfyUI with `--lowvram`. After installation it verifies `torch.cuda.is_available()` before enabling the service. ComfyUI listens only on `127.0.0.1:8188`; the standalone Worker reaches it through host networking.

The safe default remains `SUPPORTED_TASKS=image`. Video generation on a 6 GB card is model-dependent and should only be enabled after a chosen workflow has been tested for VRAM use. CORE dispatches jobs according to the Worker's live free-VRAM report, so use a `min_vram_mb` below the actually available value rather than the nominal 6144 MB.

Installer packages and services are declared in `installer/manifest.json`. Additional node roles can be added as manifest roles/profiles; optional idempotent setup hooks live in `installer/roles/`.

A standalone GPU node uses `docker-compose.worker.yml`; this file contains only the Worker and its log volume, so it cannot accidentally start CORE, Redis or PostgreSQL. During a WORKER-only installation, enter the token issued/configured on CORE when prompted (`VERTEP_NODE_TOKEN` for unattended installs). `CORE_ADDRESS` and `NODE_NAME` from `/etc/vertep/node.conf` override template values from `.env`.

## Telegram

1. Відкрити адмінку → **Система → Захищені інтеграції** → вставити токен бота в `telegram_bot_token`.
2. Перейти до розділу **Telegram** і вказати:
   - `PUBLIC_URL` — публічний HTTPS-адрес CORE, наприклад `https://example.com:8443`
   - `TELEGRAM_WEBHOOK_SECRET` — довільний секрет для підпису вебхука
   - `TELEGRAM_ALLOWED_CHAT_IDS` — дозволені Telegram `chat_id`, через кому
   - `TELEGRAM_ADMIN_CHAT_IDS` — адмінські чати для затвердження, через кому
3. Натиснути **Встановити webhook**.
4. Перевірити в Telegram: написати боту повідомлення. Якщо бренди не налаштовано, створюється Job без затвердження; якщо налаштовано — бот просить вибрати бренд.

Telegram updates are deduplicated by chat and message ID. A completed Telegram job sends `STATUS: READY` back to its source chat.

## Operations

```bash
vertep status
vertep start
vertep stop
vertep restart
vertep update
vertep recover
vertep rollback
```

The helpers are in `scripts/vertep`. `update` reads the installed node role and starts only applicable services. Signed releases are prepared under an immutable release directory and activated atomically. Jobs and event histories are persisted under the Job volume; Redis and PostgreSQL have persistent volumes and restart policies.

On CORE, update order is deliberately: drain workloads, create and verify backups, validate the signed release, verify the already-running database services, apply each unapplied migration/backfill, then activate and health-check the new application services. PostgreSQL and Redis are not recreated during a normal application update. If the database does not become ready or a migration fails, the new CORE is not activated.

Cluster updates use a PostgreSQL-backed rolling coordinator with global fencing. Nodes are drained in deterministic order, canary deployment requires explicit promotion, and a failed health check requests rollback to each node's recorded previous version. Resumable data backfills keep durable checkpoints and can continue after interruption.

`vertep status` remains usable on a Worker while CORE is offline: local GPU information is still shown. When CORE is reachable, the Worker obtains the shared system status through its node-scoped token instead of requiring the administrator password.

### Signed updates in the Web UI

On an installed CORE node, open **Система → Безпечне оновлення Vertep**. First select **Перевірити оновлення**; the **Встановити оновлення** button is enabled only when the signed update service reports a newer compatible release. Installation runs asynchronously, so the page may briefly lose its connection while services are activated and restarted. The persistent status shows the current and available versions, update phase, system state and the last update log.

The Web API never receives a command, repository URL or branch. It can enqueue only fixed maintenance actions such as `check`, `update`, and `restart`; emergency recovery returns the system to normal mode only after CORE, PostgreSQL, and Redis health checks pass. A root-owned systemd path unit processes privileged requests on the host. The CORE container receives no Docker socket.

Web updates are enabled by Bootstrap or the Ubuntu CORE installer (`WEB_UPDATE_ENABLED=true`) and restricted to administrators. Releases are fetched directly from the public GitHub Releases feed of `fylypovych/vertep`; each release includes a separately signed update manifest that binds its version, compatibility metadata and runtime-package SHA-256. The updater enters maintenance mode, drains active work, creates application, job, configuration, migration, and PostgreSQL backups, atomically switches the digest-pinned images, then applies the package. Failed health checks restore the previous release and environment; a durable update phase permits recovery after a power loss. The systemd timer checks for releases every six hours. Installed nodes need no GitHub credentials.

After upgrading an older installation to a release that first contains the Web updater, run `sudo ./install.sh` once to install and enable `vertep-update.path`. If an update fails its health check, use `vertep rollback` from the server console; database backups and the last known Git revision are retained under the project directory.

## API overview

- `POST /api/jobs`, `GET /api/jobs`, `PATCH /api/jobs/{id}`
- `POST /api/jobs/{id}/pause|resume|retry|regenerate|cancel|approve|publish`
- `PUT /api/jobs/{id}/uploads/references/{filename}` (raw request body)
- `GET /api/jobs/{id}/artifacts`, `POST /api/jobs/{id}/artifacts/verify`
- `GET /api/jobs/{id}/export`, `POST /api/projects/import` (ZIP)
- `GET /api/tasks/dead-letter`, `POST /api/tasks/dead-letter/{task_id}/retry`
- `DELETE /api/jobs/{id}`
- `POST /api/tasks/claim`, `POST /api/tasks/result`
- `POST /api/workers/heartbeat`, `GET /api/workers`
- `POST /api/telegram/webhook`, `POST /api/telegram/setup`
- `GET /api/system/update`, `POST /api/system/update/check|run` (administrator only for POST)
- `GET|POST /api/system/backups`, `POST /api/system/backups/{snapshot_id}/restore`
- `GET /api/system/models`, `POST /api/system/models/pull`, `DELETE /api/system/models/{name}`
- `GET /api/system/certificates`, `POST /api/system/certificates/renew`
- `GET /api/system/license`, `GET /api/system/installation-manifest`
- `GET /api/status`, `GET /api/integrations`, `GET /api/health`

Machine endpoints can be protected with `NODE_API_TOKEN`; the Web UI and administrative API use HTTP Basic authentication. Keep `.env` local—it is excluded from Git.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

Tests cover a local end-to-end MP4 and the actual distributed claim/result contract.
They also cover the video-worker artifact contract: CORE accepts validated scene clips and FFmpeg concatenates them into the final MP4. The sample `workflows/video/demo.json` targets the ComfyUI Video Helper Suite (`VHS_VideoCombine`), so executing that workflow on real hardware requires that matching custom node; its local registry and transport contract require no running server.

GitHub Actions runs the complete suite, Python compilation, shell syntax checks and Compose configuration validation on every push and pull request.

## Релізи та номери версій

Кожен реліз оформлюється одним основним комітом. Його назва має формат `0.0.0.94`, а цей самий коміт вміщує код, оновлений `VERSION`, секцію в `CHANGELOG.md` та файл `releases/<version>.md`. Четверта складова номера змінюється від `0` до `99`: після `0.0.0.99` іде `0.0.1.0`.

Спочатку додайте змістовні українські пункти до секції `Unreleased`, потім виконайте:

```bash
python scripts/release.py
```

Скрипт визначає наступний номер, перевіряє український опис, переносить пункти `Unreleased` до нової версії, формує нотатки, запускає перевірки, створює один готовий коміт та відправляє його у `main`. Після цього запустіть **Actions → Vertep Release → Run workflow**.

Workflow не змінює `main` і не створює другого коміту від бота. Він перевіряє готовий коміт, збирає образи й підписані артефакти, ставить тег саме на цей коміт та публікує GitHub Release. Наступний номер можна переглянути командою `python scripts/release.py --show-next`, а готовий коміт перевірити командою `python scripts/release.py --check`.

## Orchestration and artifacts

Each Job has explicit SCRIPT, ASSETS, TTS, ASSEMBLY and PUBLISH stages. Script scenes are dispatched as independent tasks, so multiple workers may process one Job concurrently. CORE retries only the failed scene; exhausted tasks enter the dead-letter queue. Assembly starts after every scene reaches READY.

Set `scheduled_for` to an ISO-8601 timestamp in `POST /api/jobs` to defer processing. Every generated or uploaded file is recorded in `manifest.json` with its MIME type, byte size, SHA-256 digest, scene/task/worker provenance and workflow. Verified downloads reject missing or modified files.

`PATCH /api/jobs/{id}` accepts `expected_version`; a stale value returns HTTP 409. This prevents two browser sessions from silently overwriting each other's changes.

The CORE also provides priority/leased tasks with watchdog recovery, structured rotating logs, Character and Brand APIs, multi-scene FFmpeg assembly, Telegram commands, per-worker tokens, administrative sessions and mock-safe publisher contracts. Live social-network upload methods still require platform-specific API credentials and implementations.

Use `sudo ./install.sh --dry-run` for a read-only preflight, `python scripts/generate-env.py` to create unique local secrets, and `python scripts/upgrade-config.py` after upgrades to add new configuration keys without overwriting existing values. Current release metadata is stored in `VERSION` and `CHANGELOG.md`.
## Appliance runtime details

For NVIDIA hosts Bootstrap installs the recommended driver and NVIDIA Container Toolkit, registers the Docker runtime and verifies `nvidia-smi`. For AMD hosts it installs ROCm/HIP, verifies `/dev/kfd`, `/dev/dri` and `rocminfo`, and applies the signed AMD Compose overlay. GPU-specific overlays remain active during updates, rollback, watchdog restarts and startup recovery.

The production runtime has separate License Manager, Dispatcher, Scheduler and Certificate Manager services. Core waits for every selected service to become healthy. Proxy, Prometheus, Loki, Promtail and Grafana configuration is embedded into digest-pinned images rather than mounted from mutable runtime files.

The Deployment Wizard obtains its role list from `config/node_roles.json`; adding a role does not require changing token or enrollment logic. Core nodes can create 15-minute, one-use registration tokens from **Workers → Add Worker**. Non-Core nodes initiate the HTTPS enrollment request themselves and receive a node-bound JWT, per-node secret, certificate attestation, configuration, and capability set. Dispatch is capability-driven rather than role-driven, so installing a new engine only requires the node to advertise its new capability.

Release candidates must pass the reproducible appliance gates; CI uploads the resulting JSON evidence:

```bash
python scripts/qualify-release.py --root . --docker --output qualification.json
```

Integration credentials are managed from **System → Protected integrations**. Values are write-only
through the API and remain inside the authenticated encrypted secret envelope.

During First Run the selected AI backend is contacted before setup completes: Vertep validates the endpoint, HTTPS policy, credentials and model inventory, and can pull a missing local Ollama model. The final Installation Manifest records the selected role and modules, actual Docker image digests, container state and module health.

Routine appliance lifecycle is available under **System → Zero-Shell lifecycle**: administrators can create or restore encrypted backups, install or remove Ollama models, and inspect or renew the TLS certificate without using an SSH session. The authenticated `/api/system/installation-manifest` endpoint returns the current installation inventory.
