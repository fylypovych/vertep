# Vertep

Vertep is a modular content-factory orchestrator for Ubuntu Server 24.04. CORE owns jobs and dispatches GPU work; WORKER runs replaceable ComfyUI workflows and returns artifacts; CORE assembles a valid MP4 with FFmpeg.

## Demo

```bash
cp .env.example .env
# Set unique ADMIN_PASSWORD, NODE_API_TOKEN and POSTGRES_PASSWORD values.
docker compose up --build
```

Open `http://localhost:8080` and sign in as `ADMIN_USER` (default `admin`). Demo mode still crosses the CORE/WORKER task protocol, but uses a deterministic image instead of a GPU model. The resulting `jobs/<job_id>/final/video.mp4` is a real MP4.

For a local CORE-only developer run, set `LOCAL_WORKER_FALLBACK=true`. This fallback exists for development and is disabled in the supplied Compose environment.

## Ubuntu installation

Run `sudo ./install.sh`, then choose `CORE`, `GPU WORKER`, or both. The installer:

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

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and an HTTPS `PUBLIC_URL`, start CORE, then authenticate as the administrator and call:

```bash
curl -u admin -X POST http://localhost:8080/api/telegram/setup
```

Telegram updates are deduplicated by chat and message ID. A completed Telegram job sends `STATUS: READY` back to its source chat.

## Operations

```bash
vertep status
vertep update
vertep rollback
```

The helpers are in `scripts/vertep`. `update` reads the installed node role and rebuilds only applicable services. Jobs and event histories are persisted under the Job volume; Redis and PostgreSQL have persistent volumes and restart policies.

On CORE, update order is deliberately: backup, pull, start database services, wait for PostgreSQL, apply each unapplied migration, then rebuild CORE. If the database does not become ready or a migration fails, the new CORE is not started.

`vertep status` remains usable on a Worker while CORE is offline: local GPU information is still shown. When CORE is reachable, the Worker obtains the shared system status through its node-scoped token instead of requiring the administrator password.

### Updating from GitHub in the Web UI

On an installed CORE node, open **Система → Оновлення з GitHub**. First select **Перевірити оновлення**; the **Встановити оновлення** button is enabled only when the tracked upstream contains newer commits. Installation runs asynchronously, so the page may briefly lose its connection while CORE is rebuilt and restarted. The persistent status shows the current and remote revisions, ahead/behind counts, dirty-worktree state and the last update log.

The Web API never receives a command, repository URL or branch. It can enqueue only `check` or `update`. A root-owned systemd path unit processes the request on the host and invokes the existing role-aware `vertep update`, which creates backups, performs a fast-forward-only pull, applies migrations and runs health checks. The CORE container receives no Docker socket.

Web updates are enabled by the Ubuntu CORE installer (`WEB_UPDATE_ENABLED=true`) and restricted to administrators. Releases are fetched only from `VERTEP_UPDATE_SERVER` (default `https://update.vertep.ai`) and their detached signature is verified with `UPDATE_PUBLIC_KEY` before a package is accepted. The updater enters maintenance mode, drains active work, creates application, job, configuration, migration, and PostgreSQL backups, then applies the package. Failed health checks trigger an automatic rollback; a durable update phase permits recovery after a power loss. The systemd timer checks for releases every six hours. GitHub credentials and repository access are never required on an installed node.

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

## Releases and version numbers

Release versions are strictly sequential: `0.0.0.1`, `0.0.0.2`, `0.0.0.3`, and so on. Do not edit `VERSION` manually and do not use a normal `git push` for a release. Run:

```bash
python scripts/release.py
```

The command fetches tags, selects one greater than the highest `v0.0.0.N` tag, stages the repository, scans staged files for common secrets, moves notes from `CHANGELOG.md` → `Unreleased` into the new version section, generates `releases/<version>.md`, runs compilation and all tests, creates a release commit and annotated tag, and atomically pushes both to `origin`.

If `Unreleased` is empty, the description is generated automatically from commit subjects and changed project areas. Use `python scripts/release.py --show-next` to inspect the next number or `--no-push` to create the commit and tag locally. GitHub also provides **Actions → Create sequential release → Run workflow**, which executes the same process with serialized concurrency.

## Orchestration and artifacts

Each Job has explicit SCRIPT, ASSETS, TTS, ASSEMBLY and PUBLISH stages. Script scenes are dispatched as independent tasks, so multiple workers may process one Job concurrently. CORE retries only the failed scene; exhausted tasks enter the dead-letter queue. Assembly starts after every scene reaches READY.

Set `scheduled_for` to an ISO-8601 timestamp in `POST /api/jobs` to defer processing. Every generated or uploaded file is recorded in `manifest.json` with its MIME type, byte size, SHA-256 digest, scene/task/worker provenance and workflow. Verified downloads reject missing or modified files.

`PATCH /api/jobs/{id}` accepts `expected_version`; a stale value returns HTTP 409. This prevents two browser sessions from silently overwriting each other's changes.

The CORE also provides priority/leased tasks with watchdog recovery, structured rotating logs, Character and Brand APIs, multi-scene FFmpeg assembly, Telegram commands, per-worker tokens, administrative sessions and mock-safe publisher contracts. Live social-network upload methods still require platform-specific API credentials and implementations.

Use `sudo ./install.sh --dry-run` for a read-only preflight, `python scripts/generate-env.py` to create unique local secrets, and `python scripts/upgrade-config.py` after upgrades to add new configuration keys without overwriting existing values. Current release metadata is stored in `VERSION` and `CHANGELOG.md`.
## Appliance installation

On a clean Ubuntu Server 24.04 host, the supported production installation is a single bootstrap command:

```bash
curl -fsSL https://download.vertep.ai/bootstrap.sh | sudo bash
```

Bootstrap validates hardware and connectivity, installs the container runtime, downloads checksum-verified immutable runtime metadata from Vertep, generates all local credentials and TLS material, starts the complete stack, and waits for its health endpoint. Open the printed `https://SERVER-IP:8443` address to finish the seven-step First Run Wizard. After that, routine administration and signed updates are performed from the Web UI; bootstrap is not used again. The source-oriented `install.sh` remains available for development and advanced node deployments.

The Deployment Wizard obtains its role list from `config/node_roles.json`; adding a role does not require changing token or enrollment logic. Core nodes can create 15-minute, one-use registration tokens from **Workers → Add Worker**. Non-Core nodes initiate the HTTPS enrollment request themselves and receive a node-bound JWT, per-node secret, certificate attestation, configuration, and capability set. Dispatch is capability-driven rather than role-driven, so installing a new engine only requires the node to advertise its new capability.

Production readiness and the traceability status of all three deployment/update specifications are tracked in [`docs/REQUIREMENTS_GAP_ANALYSIS_UK.md`](docs/REQUIREMENTS_GAP_ANALYSIS_UK.md). Items marked as partial or missing there must not be presented as completed appliance functionality.

Release candidates must pass the reproducible appliance gates; CI uploads the resulting JSON evidence:

```bash
python scripts/qualify-release.py --root . --docker --output qualification.json
```

Integration credentials are managed from **System → Protected integrations**. Values are write-only
through the API and remain inside the authenticated encrypted secret envelope.
