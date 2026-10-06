import os
import time
import base64
import subprocess
import json
import shutil
import tempfile
import platform
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from adapters.comfyui import PromptCancelledError
from adapters import comfyui_runtime
from adapters.providers import providers
from adapters.providers.base import ComputeProvider
from core.gpu_profiles import gpu_profile
from core.logging_config import configure_logging
from worker.role_executor import execute_role_task

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = configure_logging("worker")
pending_logs: list[dict] = []

def verify_gpu_runtime_inventory() -> dict | None:
    """Check the GPU node against its pinned ComfyUI runtime inventory.

    Fail-closed: a missing inventory, a component that drifted from its pin or a
    workflow/model whose checksum no longer matches must keep the node out of
    dispatch, because an unpinned GPU runtime silently changes what Vertep
    produces.  Returns ``None`` when no inventory is configured, which keeps a
    bare development checkout working.
    """
    if not comfyui_runtime.inventory_path():
        return None
    return comfyui_runtime.verify_runtime(comfyui_runtime.installed_components())


def role_self_test(role: str, metrics: dict, adapter: ComputeProvider | None = None) -> dict:
    started = time.monotonic()
    try:
        if role == "core":
            additional = {item.strip() for item in os.getenv("NODE_ADDITIONAL_ROLES", "").split(",") if item.strip()}
            if not additional:
                raise RuntimeError("Core has no local worker roles")
            health_urls = {
                "gpu": os.getenv("COMFYUI_HEALTH_URL", "http://comfyui:8188/system_stats"),
                "text": os.getenv("OLLAMA_HEALTH_URL", "http://ollama:11434/api/tags"),
                "voice": os.getenv("TTS_HEALTH_URL", "http://tts:8090/health"),
                "publisher": os.getenv("PUBLISHER_HEALTH_URL", "http://publisher-worker:8091/health"),
                "monitoring": os.getenv("PROMETHEUS_URL", "http://monitoring:9090/-/healthy"),
            }
            for selected in additional & health_urls.keys():
                httpx.get(health_urls[selected], timeout=15).raise_for_status()
            if "backup" in additional:
                Path(os.getenv("BACKUP_ROOT", str(_PROJECT_ROOT / "backups"))).mkdir(parents=True, exist_ok=True)
        elif role == "gpu":
            if os.getenv("DEMO_MODE", "true").lower() != "true" and not metrics.get("gpu_available"):
                raise RuntimeError("NVIDIA GPU/driver is unavailable")
            verify_gpu_runtime_inventory()
            adapter = adapter or providers.compute()
            data, _, kind = adapter.generate_output(os.getenv("SELF_TEST_WORKFLOW", "workflows/image/demo.json"),
                                                     "Vertep worker self-test", "image")
            if kind != "image" or len(data) < 16:
                raise RuntimeError("GPU workflow returned no image")
        elif role == "text":
            response = httpx.post(f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/generate",
                                  json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"),
                                        "prompt": "Reply OK", "stream": False}, timeout=60)
            response.raise_for_status()
            if not response.json().get("response"):
                raise RuntimeError("Ollama returned no text")
        elif role == "voice":
            url = os.getenv("TTS_HEALTH_URL")
            if not url or httpx.get(url, timeout=15).status_code >= 400:
                raise RuntimeError("TTS runtime health check failed")
            synthesis_url = url.replace("/health", "/synthesize") if url.endswith("/health") else url.rstrip("/") + "/synthesize"
            response = httpx.post(synthesis_url, json={"text": "OK", "voice": os.getenv("TTS_VOICE", "default")}, timeout=60)
            response.raise_for_status()
            if "audio" not in response.headers.get("content-type", "").lower():
                encoded = response.json().get("audio_base64")
                if not isinstance(encoded, str) or not encoded:
                    raise RuntimeError("TTS runtime did not return audio")
        elif role == "publisher":
            if not os.getenv("PUBLISHER_HEALTH_URL"):
                raise RuntimeError("Publisher runtime is not configured")
            httpx.get(os.environ["PUBLISHER_HEALTH_URL"], timeout=15).raise_for_status()
            publish_url = os.environ["PUBLISHER_HEALTH_URL"].replace("/health", "/publish") if os.environ["PUBLISHER_HEALTH_URL"].endswith("/health") else os.environ["PUBLISHER_HEALTH_URL"].rstrip("/") + "/publish"
            response = httpx.post(publish_url, json={"job_id": "self-test", "payload": {"topic": "self-test"}}, timeout=30)
            if response.status_code == 503:
                raise RuntimeError("Publisher adapter not configured")
            response.raise_for_status()
            receipt = response.json()
            if not receipt.get("publication_id"):
                raise RuntimeError("Publisher returned no receipt")
        elif role == "monitoring":
            httpx.get(os.getenv("PROMETHEUS_URL", "http://monitoring:9090/-/healthy"), timeout=15).raise_for_status()
        elif role == "backup":
            root = Path(os.getenv("BACKUP_ROOT", str(_PROJECT_ROOT / "backups")))
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
                output.write(b"vertep-backup-self-test")
                output.flush()
                os.fsync(output.fileno())
                path = Path(output.name)
            if path.read_bytes() != b"vertep-backup-self-test":
                raise RuntimeError("Backup storage read-after-write failed")
            path.unlink()
        else:
            raise RuntimeError(f"No self-test is implemented for role {role}")
        return {"status": "PASSED", "role": role, "checked_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000)}
    except Exception as error:
        return {"status": "FAILED", "role": role, "checked_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000), "error": str(error)[:500]}


def bind_self_test(test: dict, request: dict | None, version: str | None) -> dict:
    """Echo the active bound self-test request into a freshly produced result.

    Only a result carrying the nonce/operation/target of the request CORE is
    waiting for may approve the current rollout phase.
    """
    if isinstance(request, dict) and request.get("nonce"):
        test["nonce"] = request.get("nonce")
        test["operation_id"] = request.get("operation_id")
        test["target_version"] = version or request.get("target_version")
    return test


def configured_role() -> str:
    role = os.getenv("NODE_ROLE", "gpu")
    if role != "unassigned":
        return role
    try:
        return str(json.loads((Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json")).parent
                               / "installation.json").read_text())["node_role"])
    except (OSError, ValueError, KeyError):
        return "gpu"

def node_capabilities() -> list[str]:
    # Issue #122 P5: a GPU node also finishes the assembly of an external engine,
    # so it advertises video generation and video assembly.
    defaults = {"gpu": "image_generation,image_upscale,controlnet,inpainting,video_generation,video_assembly",
                "text": "text_generation", "voice": "speech_synthesis",
                "publisher": "publishing", "backup": "backup,snapshot,archive",
                "monitoring": "metrics,logs,alerting"}
    role = configured_role()
    raw = os.getenv("NODE_CAPABILITIES")
    if not raw:
        raw = defaults.get(role, "")
    return sorted({item.strip() for item in raw.split(",") if item.strip()})


def voice_catalog() -> dict:
    """Advertise which voices/models this node can synthesize.

    Driven by ``TTS_VOICES`` / ``TTS_MODELS`` (comma-separated) or the live TTS
    runtime's ``/voices`` catalog when available.  An empty result means the
    node accepts any voice request (backward compatible with existing workers);
    a non-empty ``voices`` / ``models`` list lets CORE pick ready nodes and
    refuse to dispatch a voice to a node that cannot honour the character's
    voice/model requirements.
    """
    voices = [item.strip() for item in os.getenv("TTS_VOICES", "").split(",") if item.strip()]
    models = [item.strip() for item in os.getenv("TTS_MODELS", "").split(",") if item.strip()]
    if not voices and not models:
        url = os.getenv("TTS_URL", "http://tts:8090").rstrip("/")
        try:
            catalog = httpx.get(f"{url}/voices", timeout=10).json()
            if isinstance(catalog, dict):
                voices = [str(item.get("id", item)) for item in catalog.get("voices", [])]
        except (httpx.HTTPError, ValueError):
            voices = []
    result: dict[str, list[str]] = {}
    if voices:
        result["voices"] = sorted(set(voices))
    if models:
        result["models"] = sorted(set(models))
    return result


_MODEL_CATALOG_CACHE: dict = {"value": {}, "loaded": False, "checked_at": 0.0}
_MODEL_COMMANDS: dict[str, dict] = {}
_MODEL_COMMANDS_LOCK = threading.Lock()
_MODEL_COMMAND_LIMIT = 64


def text_model_catalog() -> dict:
    """Cached Ollama model catalog advertised to CORE in the heartbeat.

    Only text-capable nodes expose a catalog; the refresh interval keeps the
    heartbeat cheap while still letting CORE observe placement changes.
    """
    if configured_role() != "text" and "text_generation" not in node_capabilities():
        return {}
    interval = float(os.getenv("MODEL_CATALOG_INTERVAL_SECONDS", "30"))
    now = time.monotonic()
    if _MODEL_CATALOG_CACHE["loaded"] and now - _MODEL_CATALOG_CACHE["checked_at"] < interval:
        return _MODEL_CATALOG_CACHE["value"]
    try:
        response = httpx.get(f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/tags", timeout=15)
        response.raise_for_status()
        models = sorted({str(item.get("name")) for item in response.json().get("models", [])
                         if isinstance(item, dict) and item.get("name")})
        _MODEL_CATALOG_CACHE["value"] = {"models": models}
        _MODEL_CATALOG_CACHE["loaded"] = True
        _MODEL_CATALOG_CACHE["checked_at"] = now
    except (httpx.HTTPError, ValueError, AttributeError):
        if not _MODEL_CATALOG_CACHE["loaded"]:
            return {}
    return _MODEL_CATALOG_CACHE["value"]


def _report_model_progress(client: httpx.Client, core: str, node_name: str,
                           operation_id: str, status: str, phase: str,
                           progress: int, error: str | None = None) -> bool:
    """Send one progress sample to CORE; True means CORE wants us to stop."""
    try:
        response = client.post(f"{core}/api/workers/model-progress", json={
            "node_name": node_name, "operation_id": operation_id,
            "status": status, "phase": phase[:120],
            "progress": max(0, min(100, int(progress))),
            "error": str(error)[:2000] if error else None,
        })
        response.raise_for_status()
        return bool(response.json().get("cancel_requested"))
    except (httpx.HTTPError, ValueError, KeyError):
        return False


def _run_model_command(core: str, node_name: str, command: dict,
                       client_options: dict, cancel_event: threading.Event) -> None:
    """Execute a node-local Ollama pull/delete and stream progress to CORE."""
    operation_id = str(command.get("operation_id") or "")
    action = str(command.get("action") or "")
    model = str(command.get("model") or "")
    base = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
    state = {"last_progress": 0, "last_sent": 0.0}

    def report(status: str, phase: str, progress: int,
               error: str | None = None, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and status == "RUNNING":
            if progress < state["last_progress"]:
                return False
            if now - state["last_sent"] < 1.0 and progress - state["last_progress"] < 2:
                return False
        stopped = _report_model_progress(client, core, node_name, operation_id,
                                         status, phase, progress, error)
        state["last_progress"] = max(state["last_progress"], progress)
        state["last_sent"] = now
        return stopped

    def stop_requested(status: str = "RUNNING", phase: str = "pull",
                       progress: int | None = None) -> bool:
        if cancel_event.is_set():
            report("CANCELLED", phase, state["last_progress"] if progress is None else progress,
                   "cancelled by CORE", force=True)
            return True
        if report(status, phase, state["last_progress"] if progress is None else progress):
            cancel_event.set()
            report("CANCELLED", phase, state["last_progress"] if progress is None else progress,
                   "cancelled by CORE", force=True)
            return True
        return False

    client: httpx.Client | None = None
    try:
        client = httpx.Client(**client_options)
        if action == "cancel":
            report("CANCELLED", "cancel", state["last_progress"], "cancelled by CORE", force=True)
            return
        if action == "delete":
            if stop_requested("RUNNING", "delete", 10):
                return
            response = client.request("DELETE", f"{base}/api/delete",
                                      json={"name": model}, timeout=120)
            if response.status_code >= 400:
                report("FAILED", "delete", 10,
                       f"Ollama returned {response.status_code}: {response.text[:300]}",
                       force=True)
                return
            report("COMPLETED", "delete", 100, force=True)
            return
        if action != "pull":
            report("FAILED", action or "unknown", 0,
                   f"Unsupported model command action: {action}", force=True)
            return

        if stop_requested("RUNNING", "pull", 0):
            return
        with client.stream("POST", f"{base}/api/pull",
                           json={"name": model, "stream": True},
                           timeout=None) as response:
            if response.status_code != 200:
                body = response.read()[:500].decode("utf-8", "replace")
                report("FAILED", "pull", state["last_progress"],
                       f"Ollama returned {response.status_code}: {body}", force=True)
                return
            for line in response.iter_lines():
                if cancel_event.is_set():
                    report("CANCELLED", "pull", state["last_progress"],
                           "cancelled by CORE", force=True)
                    return
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    report("FAILED", "pull", state["last_progress"],
                           str(event["error"])[:500], force=True)
                    return
                completed, total = event.get("completed"), event.get("total")
                if completed is not None and total:
                    percent = max(0, min(100, int(completed * 100 // total)))
                    if stop_requested("RUNNING", "pull", percent):
                        return
                    state["last_progress"] = max(state["last_progress"], percent)
                elif event.get("status"):
                    if stop_requested("RUNNING", str(event["status"])[:120]):
                        return
        report("COMPLETED", "pull", 100, force=True)
    except Exception as error:  # noqa: BLE001 - a transport failure must surface durably
        report("FAILED", action or "pull", state["last_progress"], str(error)[:500], force=True)
    finally:
        if client is not None:
            client.close()
        with _MODEL_COMMANDS_LOCK:
            entry = _MODEL_COMMANDS.get(operation_id)
            if entry is not None and entry["cancel"] is cancel_event:
                _MODEL_COMMANDS.pop(operation_id, None)


def handle_model_command(command: dict | None, core: str, node_name: str,
                         client_options: dict) -> None:
    """Start (or cancel) the thread that executes a heartbeat model command."""
    if not isinstance(command, dict):
        return
    operation_id = str(command.get("operation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        return
    action = str(command.get("action") or "")
    with _MODEL_COMMANDS_LOCK:
        entry = _MODEL_COMMANDS.get(operation_id)
        if entry is not None:
            if action == "cancel":
                entry["cancel"].set()
                if entry["thread"].is_alive():
                    return
            else:
                # Already executed once; never run the same placement twice.
                return
        if len(_MODEL_COMMANDS) > _MODEL_COMMAND_LIMIT:
            for stale_id, stale in list(_MODEL_COMMANDS.items()):
                if not stale["thread"].is_alive():
                    _MODEL_COMMANDS.pop(stale_id, None)
        cancel_event = threading.Event()
        if action == "cancel" and entry is not None:
            cancel_event = entry["cancel"]
        thread = threading.Thread(target=_run_model_command,
                                  args=(core, node_name, command, client_options, cancel_event),
                                  daemon=True, name=f"model-{operation_id[:8]}")
        _MODEL_COMMANDS[operation_id] = {"thread": thread, "cancel": cancel_event}
    thread.start()

def _ensure_csr(node_name: str, pki: Path, csr_path: Path | None = None) -> str:
    """Generate the node key locally and return only its CSR for Core signing.

    The ``csr_provider`` argument lets tests supply a stub instead of the
    real ``openssl`` binary, so the enrollment retry policy can be exercised
    without a PKI toolchain on the test host.
    """
    csr_path = csr_path or pki / "node.csr"
    if csr_path.is_file():
        return csr_path.read_text(encoding="utf-8")
    csr_provider = os.getenv("CSR_PROVIDER")
    if csr_provider:
        return csr_provider  # tests inject a fixed CSR string
    key, csr = pki / "node.key", pki / "node.csr"
    if not key.exists():
        subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={node_name}",
                        "-keyout", str(key), "-out", str(csr)], check=True, capture_output=True)
        os.chmod(key, 0o600)
    elif not csr.exists():
        subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_name}",
                        "-out", str(csr)], check=True, capture_output=True)
    return csr.read_text(encoding="utf-8")


def _enrollment_id(pki: Path) -> str:
    """Return a stable, locally persisted enrollment id used as a retry key.

    The key survives process restarts so a node whose enrollment response was
    lost can retry idempotently instead of deadlocking on its own burned
    single-use token.
    """
    path = pki / "enrollment.id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", existing):
            return existing
    except OSError:
        pass
    value = f"{node_id_slug()}-{secrets.token_hex(8)}"
    pki.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return value


def node_id_slug() -> str:
    """Node id prefix used for the locally generated enrollment id."""
    return re.sub(r"[^a-z0-9-]", "-", str(os.getenv("NODE_NAME", "vertep-node")).lower()).strip("-") or "vertep-node"


def _retry_enrollment(client: httpx.Client, core: str, node_name: str, metrics: dict,
                     capabilities: list[str], config_path: Path | None = None) -> str:
    """Submit the enrollment request with bounded lost-response retry.

    Disposable nodes (single-use registration tokens, short-lived CSR) must not
    be lost because a transient transport failure arrived between the local key
    generation and the Core acknowledgement.  Two failure classes are handled:

    * transport-level failures and transient 5xx/429 are retried, because the
      request may never have reached Core;
    * a lost *response* — Core committed the enrollment but the answer never
      arrived — is retried with the same ``enrollment_id`` idempotency key, so
      Core re-issues credentials for this node instead of rejecting the burned
      token.

    A rejected token, a malformed response or any other permanent 4xx is
    surfaced immediately so the operator sees the real problem instead of a
    retry loop.
    """
    config_path = Path(config_path or os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    registration_token = os.getenv("REGISTRATION_TOKEN", "")
    if not registration_token:
        return os.getenv("NODE_API_TOKEN", "")
    memory_mb = None
    try:
        memory_mb = int(Path("/proc/meminfo").read_text().split("MemTotal:", 1)[1].split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    pki = config_path.parent / "pki"
    pki.mkdir(parents=True, exist_ok=True)
    csr = _ensure_csr(node_name, pki)
    enrollment_id = _enrollment_id(pki)

    max_attempts = max(1, int(os.getenv("ENROLLMENT_RETRY_ATTEMPTS", "5")))
    base_delay = max(0.05, float(os.getenv("ENROLLMENT_RETRY_BASE_SECONDS", "0.5")))
    transient_statuses = {429, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(f"{core}/api/nodes/register", json={
                "registration_token": registration_token, "node_id": node_name,
                "enrollment_id": enrollment_id,
                "capabilities": capabilities, "version": os.getenv("VERTEP_VERSION", "unknown"),
                "csr": csr,
                "hardware": {**metrics, "ram_mb": memory_mb,
                             "disk_free_mb": shutil.disk_usage("/").free // 1024 // 1024}}, timeout=30)
            status = getattr(response, "status_code", 200)
            if status in transient_statuses and attempt < max_attempts:
                raise httpx.HTTPStatusError(
                    f"transient enrollment status {status}",
                    request=getattr(response, "request", None), response=response)
            if status >= 400:
                # Permanent client errors (invalid token, malformed request) are
                # surfaced immediately: retrying them would only burn the
                # single-use token and confuse the operator about the real cause.
                raise httpx.HTTPStatusError(
                    f"enrollment rejected with status {status}",
                    request=getattr(response, "request", None), response=response)
            credentials = response.json()
            (pki / "node.crt").write_text(credentials["certificate"], encoding="utf-8")
            (pki / "node-ca.crt").write_text(credentials["core_certificate"], encoding="utf-8")
            config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = config_path.with_name(f".{config_path.name}.tmp")
            temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(config_path)
            return str(credentials["jwt"])
        except httpx.HTTPStatusError as error:
            # Permanent 4xx must never be retried: only transient 5xx/429 and
            # transport-level failures reach the retry path below.
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and status not in transient_statuses:
                raise
            last_error = error
            if attempt >= max_attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
        except (httpx.HTTPError, httpx.TimeoutException) as error:
            last_error = error
            if attempt >= max_attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
    # Unreachable: the loop always raises or returns.
    raise last_error if last_error else RuntimeError("enrollment produced no result")  # pragma: no cover


def enroll(client: httpx.Client, core: str, node_name: str, metrics: dict,
           capabilities: list[str]) -> str:
    config_path = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    try:
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        if stored.get("jwt"):
            return str(stored["jwt"])
    except (OSError, ValueError):
        pass
    return _retry_enrollment(client, core, node_name, metrics, capabilities, config_path)

def renew_if_needed(core: str, node_name: str, credential: str, verify: str | bool,
                    pki: Path, config_path: Path) -> str:
    certificate, key, csr = pki / "node.crt", pki / "node.key", pki / "node.csr"
    if not certificate.is_file() or not key.is_file():
        return credential
    threshold = int(os.getenv("CERTIFICATE_RENEW_BEFORE_SECONDS", str(30 * 86400)))
    valid = subprocess.run(["openssl", "x509", "-checkend", str(threshold), "-noout",
                            "-in", str(certificate)], capture_output=True, check=False).returncode == 0
    if valid:
        return credential
    subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_name}",
                    "-out", str(csr)], check=True, capture_output=True)
    try:
        renewal_credential = json.loads(config_path.read_text(encoding="utf-8")).get("worker_secret") or credential
    except (OSError, ValueError):
        renewal_credential = credential
    with httpx.Client(timeout=30, verify=verify, cert=(str(certificate), str(key)),
                      headers={"X-Vertep-Token": renewal_credential}) as client:
        response = client.post(f"{core}/api/nodes/{node_name}/renew",
                               json={"csr": csr.read_text(encoding="utf-8")})
        response.raise_for_status()
    credentials = response.json()
    certificate.write_text(credentials["certificate"], encoding="utf-8")
    (pki / "node-ca.crt").write_text(credentials["core_certificate"], encoding="utf-8")
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    return str(credentials["jwt"])

def submit_result(client: httpx.Client, core: str, result: dict) -> None:
    response = client.post(f"{core}/api/tasks/result", json=result, timeout=90)
    if response.status_code == 409:
        return
    if response.status_code in {400, 413, 422} and result.get("success"):
        rejection = {"job_id": result["job_id"], "task_id": result["task_id"],
                     "node_name": result["node_name"], "success": False,
                     "error": f"CORE rejected generated artifacts: {response.text[:500]}"}
        failed = client.post(f"{core}/api/tasks/result", json=rejection, timeout=90)
        if failed.status_code != 409:
            failed.raise_for_status()
        return
    response.raise_for_status()

def gpu_info() -> dict:
    info = {"gpu_name": os.getenv("GPU_NAME", "unknown"),
            "gpu_count": int(os.getenv("GPU_COUNT", "1")),
            "vram_mb": int(os.getenv("VRAM_MB", "0")),
            "cuda_version": os.getenv("CUDA_VERSION", "unknown"),
            "gpu_available": False}
    try:
        query = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,temperature.gpu,utilization.gpu,driver_version,compute_cap",
                                "--format=csv,noheader,nounits"], check=True, capture_output=True,
                               text=True, timeout=10).stdout.strip().splitlines()
        rows = [[part.strip() for part in line.split(",")] for line in query if line.strip()]
        if rows:
            profile = gpu_profile(rows[0][0], int(rows[0][1]), rows[0][6])
            info.update({"gpu_name": rows[0][0], "gpu_count": len(rows),
                         "vram_mb": sum(int(row[1]) for row in rows),
                         "free_vram_mb": sum(int(row[2]) for row in rows),
                         "temperature": max(float(row[3]) for row in rows),
                         "gpu_load": max(float(row[4]) for row in rows), "driver_version": rows[0][5],
                         "compute_capability": rows[0][6], "gpu_architecture": profile["architecture"],
                         "gpu_profile": profile["profile_id"], "gpu_available": True})
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("nvidia-smi metrics unavailable")
    return info


def host_metrics() -> dict:
    memory_mb = None
    try:
        values = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        memory_mb = int(next(line for line in values if line.startswith("MemAvailable:"))
                        .split()[1]) // 1024
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    try:
        cpu_load = os.getloadavg()[0]
    except OSError:
        cpu_load = None
    return {"ram_mb": memory_mb, "disk_free_mb": shutil.disk_usage("/").free // 1024 // 1024,
            "cpu_load": cpu_load, "runtime_version": platform.python_version()}


def cancel_fence_path() -> Path | None:
    root = os.getenv("UPDATE_REQUEST_DIR", "")
    if not root:
        return None
    return Path(root).parent / "cancel-fence.json"


def sync_cancel_fence(control: dict) -> dict | None:
    """Persist CORE's cancel verdict on this node so host apply can be fenced.

    CORE stamps the token into the worker record during reconciliation and
    returns it here; storing it is this node's acknowledgement of the cancel,
    and it stays authoritative until CORE reports a rollout state other than
    ``CANCELLED``.  The returned mapping is the heartbeat ``cancel_fence_ack``
    CORE requires before this agent may mutate rollout state again.
    """
    path = cancel_fence_path()
    if control.get("rollout_state") != "CANCELLED":
        if path is not None:
            path.unlink(missing_ok=True)
        return None
    token = control.get("cancel_fence_token")
    if not token:
        return None
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "state": "CANCELLED",
            "operation_id": control.get("rollout_operation_id"),
            "cancel_fence_token": token,
            "observed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        temporary.replace(path)
    return {"operation_id": control.get("rollout_operation_id"),
            "cancel_fence_token": token,
            "acknowledged_at": datetime.now(timezone.utc).isoformat()}


def request_local_update(target_version: str, action: str = "update",
                         request_id: str | None = None) -> None:
    request_root = os.getenv("UPDATE_REQUEST_DIR", "")
    if not request_root:
        raise RuntimeError("UPDATE_REQUEST_DIR is required for coordinated rolling updates")
    root = Path(request_root)
    root.mkdir(parents=True, exist_ok=True)
    fence_path = root.parent / "cancel-fence.json"
    if fence_path.exists():
        try:
            fence = json.loads(fence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Mutation blocked: cancel fence state is unreadable") from error
        raise RuntimeError(
            "Mutation blocked: rollout operation "
            f"{fence.get('operation_id')} was cancelled and this agent must not "
            "queue another host apply")
    marker = root.parent / "worker-update-target"
    if request_id and not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("request_id must be 32 lowercase hexadecimal characters")
    marker_value = target_version if action == "update" else f"{action}:{target_version}"
    if request_id:
        marker_value = f"{marker_value}:{request_id}"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == marker_value:
        return
    request_id = request_id or secrets.token_hex(16)
    temporary = root / f".{request_id}.tmp"
    temporary.write_text(json.dumps({"request_id": request_id, "action": action,
                                     "target_version": target_version}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(root / f"{request_id}.json")
    marker.write_text(marker_value, encoding="utf-8")

def worker_status(metrics: dict, require_gpu: bool, busy: bool = False) -> str:
    if require_gpu and not metrics.get("gpu_available", False):
        return "ERROR"
    return "BUSY" if busy else "READY"

def execute_task(adapter: ComputeProvider, task: dict, node_name: str) -> dict:
    try:
        artifacts = execute_role_task(configured_role(), task)
        images = [{"filename": item["filename"], "image_base64": item["data_base64"]}
                  for item in artifacts if item["kind"] == "image"]
        return {"job_id": task["job_id"], "task_id": task["task_id"], "node_name": node_name, "success": True,
                "filename": artifacts[0]["filename"],
                "image_base64": images[0]["image_base64"] if images else None,
                "images": images, "artifacts": artifacts}
    except PromptCancelledError as error:
        # CORE requested the cancellation: this is a terminal, expected outcome and
        # must not be reported as a failure (which would trigger a pointless retry
        # of a scene the operator already abandoned).
        logger.info("Task cancelled by CORE", extra={"job_id": task.get("job_id"), "node_name": node_name})
        pending_logs.append({"level": "INFO", "message": str(error), "job_id": task.get("job_id")})
        return {"job_id": task["job_id"], "task_id": task["task_id"], "node_name": node_name,
                "success": False, "cancelled": True, "error": str(error)}
    except Exception as error:
        logger.exception("Worker task failed", extra={"job_id": task.get("job_id"), "node_name": node_name})
        pending_logs.append({"level": "ERROR", "message": str(error), "job_id": task.get("job_id")})
        return {"job_id": task["job_id"], "task_id": task["task_id"], "node_name": node_name, "success": False, "error": str(error)}

def cancel_video_engine(submit_key: str | None = None, *, task_id: str | None = None) -> None:
    """Ask the effective engine runtime to stop an external assembly attempt.

    The attempt is addressed by its durable submit key, which is the identity CORE
    owns for the whole attempt and which survives a lost submit response; the
    transient CORE task id is only a fallback. A pinned runtime that is still
    processing answers with 409; that is reported as "still running" and the
    attempt stays cancelled logically while CORE discards any late result (Issue
    #122 §5, §9.8). Failure to reach the runtime must not raise here: the attempt
    is already fenced by CORE.
    """
    try:
        engine = providers.video_engine()
        if getattr(engine, "engine_id", "native") == "native":
            return
        stopped = engine.cancel(task_id, submit_key=submit_key) if hasattr(engine, "cancel") else False
        logger.info("Assembly cancellation forwarded to the engine runtime",
                    extra={"task_id": task_id, "submit_key": submit_key, "stopped": bool(stopped)})
    except Exception as error:  # noqa: BLE001 - cancellation is best effort
        logger.warning("Assembly cancellation could not be forwarded",
                       extra={"task_id": task_id, "error": str(error)})


def claim_video_engine_state() -> dict:
    """Effective engine configuration and readiness of this node (Issue #122 P5/P7).

    CORE decides an assembly attempt against one exact engine configuration and hands
    it only to a node that reports the same one with a ready runtime. Reporting the
    non-secret snapshot fields plus the readiness verdict is what makes that check
    possible across processes: a CORE-side registry says nothing about what this node
    can actually run.
    """
    try:
        from core.engine_config import engine_snapshot_fields, verify_effective_engine

        engine = providers.video_engine()
        report = verify_effective_engine(probe=True, engine=engine)
        return {
            **engine_snapshot_fields(engine),
            "ready": report["ready"],
            "reason": report["reason"],
        }
    except Exception as error:  # noqa: BLE001 - an unreadable state is reported, not hidden
        return {
            "engine_id": "unknown",
            "ready": False,
            "reason": f"engine_state_error:{type(error).__name__}",
        }


def main() -> None:
    core = os.getenv("CORE_ADDRESS", "http://localhost:8080")
    supported_tasks = [item.strip() for item in os.getenv("SUPPORTED_TASKS", "image").split(",") if item.strip()]
    supported_workflows = [item.strip() for item in os.getenv("SUPPORTED_WORKFLOWS", "*").split(",") if item.strip()]
    metrics = gpu_info()
    require_gpu = os.getenv("WORKER_REQUIRE_GPU", "true").lower() == "true" and os.getenv("DEMO_MODE", "true").lower() != "true"
    capabilities = node_capabilities()
    payload = {"node_name": os.getenv("NODE_NAME", "gpu-01"),
               "runtime_instance_id": uuid.uuid4().hex, **metrics, **host_metrics(),
               "status": worker_status(metrics, require_gpu),
               "supported_tasks": supported_tasks, "supported_workflows": supported_workflows,
               "role": configured_role(), "capabilities": capabilities,
               "voice_catalog": voice_catalog() if "speech_synthesis" in capabilities else {},
               "version": os.getenv("VERTEP_VERSION")}
    adapter = providers.compute()
    self_test = role_self_test(configured_role(), metrics, adapter)
    payload["self_test"] = self_test
    if self_test["status"] != "PASSED":
        payload["status"] = "ERROR"
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    active_task = None
    enrollment_verify = os.getenv("CORE_CA_PATH", "")
    if core.startswith("https://") and os.getenv("REGISTRATION_TOKEN") and not enrollment_verify:
        raise RuntimeError("CORE_CA_PATH is required to pin Core before sending a registration token")
    with httpx.Client(timeout=5, verify=enrollment_verify or True) as enrollment_client:
        credential = enroll(enrollment_client, core, payload["node_name"], metrics, capabilities)
    pki = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json")).parent / "pki"
    config_path = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    credential = renew_if_needed(core, payload["node_name"], credential, enrollment_verify or True,
                                 pki, config_path)
    client_options = {"timeout": 5, "headers": {"X-Vertep-Token": credential} if credential else {}}
    if core.startswith("https://") and (pki / "node.crt").is_file():
        client_options.update({"verify": enrollment_verify or True,
                               "cert": (str(pki / "node.crt"), str(pki / "node.key"))})
    with httpx.Client(**client_options) as client:
        desired_state = None
        active_self_test_request: dict | None = None
        next_self_test = time.monotonic() + (15 if self_test["status"] != "PASSED"
                                             else float(os.getenv("SELF_TEST_INTERVAL_SECONDS", "300")))
        while True:
            try:
                metrics = gpu_info()
                payload.update(metrics)
                payload.update(host_metrics())
                payload["model_catalog"] = text_model_catalog()
                if future is None and time.monotonic() >= next_self_test:
                    payload["self_test"] = bind_self_test(
                        role_self_test(configured_role(), metrics, adapter),
                        active_self_test_request, payload.get("version"))
                    next_self_test = time.monotonic() + (15 if payload["self_test"]["status"] != "PASSED"
                                                         else float(os.getenv("SELF_TEST_INTERVAL_SECONDS", "300")))
                if payload.get("self_test", {}).get("status") != "PASSED":
                    payload.update({"status": "ERROR", "current_job": None, "current_task": None})
                    client.post(f"{core}/api/workers/heartbeat", json=payload).raise_for_status()
                    time.sleep(15)
                    continue
                if future is None and worker_status(metrics, require_gpu) == "ERROR":
                    payload.update({"status": "ERROR", "current_job": None, "current_task": None})
                    client.post(f"{core}/api/workers/heartbeat", json=payload).raise_for_status()
                    time.sleep(15)
                    continue
                if future is None:
                    payload["status"] = worker_status(metrics, require_gpu)
                if future and future.done():
                    result = future.result()
                    submit_result(client, core, result)
                    payload.update({"current_job": None, "status": "READY"})
                    payload["current_task"] = None
                    future = None
                    active_task = None
                if future is None and desired_state not in {"DRAINING", "QUARANTINED", "UPDATING", "ROLLBACK", "DISABLED", "RESTARTING", "REVOKED"}:
                    # Also check node status to prevent claims on DISABLED/RESTARTING/REVOKED
                    if payload.get("status") in {"DISABLED", "RESTARTING", "REVOKED", "OFFLINE", "ERROR", "QUARANTINED"}:
                        # Don't claim tasks in these states
                        pass
                    else:
                        task = client.post(f"{core}/api/tasks/claim", json={"node_name": payload["node_name"],
                                                                          "gpu_name": payload["gpu_name"],
                                                                          "vram_mb": payload["vram_mb"],
                                                                          "free_vram_mb": payload.get("free_vram_mb"),
                                                                          "supported_tasks": supported_tasks,
                                                                          "supported_workflows": supported_workflows,
                                                                          "capabilities": capabilities,
                                                                          "video_engine": (claim_video_engine_state()
                                                                                           if "assembly" in supported_tasks
                                                                                           else None),
                                                                          "voice_catalog": payload.get("voice_catalog") or {}}).json().get("task")
                        if task:
                            logger.info("Task claimed", extra={"job_id": task["job_id"], "node_name": payload["node_name"]})
                            payload.update({"current_job": task["job_id"], "current_task": task["task_id"], "status": "BUSY"})
                            active_task = task
                            future = pool.submit(execute_task, adapter, task, payload["node_name"])
                if future is not None and active_task:
                    client.post(f"{core}/api/tasks/renew", json={"node_name": payload["node_name"],
                                                                  "task_id": active_task["task_id"]}).raise_for_status()
                    cancellations = client.get(f"{core}/api/tasks/cancellations/{payload['node_name']}").json()
                    if any(item["task_id"] == active_task["task_id"] for item in cancellations):
                        if active_task.get("task") == "assembly":
                            # Issue #122 P6: the assembly attempt talks to an
                            # external engine, so cancellation is forwarded to that
                            # runtime instead of the local compute adapter.
                            cancel_video_engine(active_task.get("submit_key"),
                                                task_id=active_task["task_id"])
                        else:
                            adapter.cancel()
                heartbeat_response = client.post(f"{core}/api/workers/heartbeat", json=payload)
                if heartbeat_response.status_code == 409:
                    detail = None
                    try:
                        detail = heartbeat_response.json().get("detail")
                    except ValueError:
                        detail = None
                    if isinstance(detail, dict) and detail.get("cancel_fence_token"):
                        payload["cancel_fence_ack"] = {
                            "operation_id": detail.get("rollout_operation_id"),
                            "cancel_fence_token": detail.get("cancel_fence_token"),
                            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
                        }
                        heartbeat_response = client.post(f"{core}/api/workers/heartbeat", json=payload)
                heartbeat_response.raise_for_status()
                control = heartbeat_response.json()
                fence_ack = sync_cancel_fence(control)
                if fence_ack:
                    payload["cancel_fence_ack"] = fence_ack
                else:
                    payload.pop("cancel_fence_ack", None)
                active_self_test_request = control.get("self_test_request")
                desired_state = control.get("desired_state")
                update_target = control.get("update_target_version")
                rollback_target = control.get("rollback_target_version")
                restart_operation_id = control.get("restart_operation_id")
                if control.get("model_command"):
                    handle_model_command(control.get("model_command"), core,
                                         payload["node_name"], client_options)
                if desired_state == "ROLLBACK" and future is None:
                    request_local_update(rollback_target or "previous", action="rollback")
                    payload["status"] = "UPDATING"
                if desired_state == "RESTARTING" and future is None:
                    request_local_update(update_target or "current", action="restart",
                                         request_id=restart_operation_id)
                    payload["status"] = "UPDATING"
                if update_target and future is None:
                    request_local_update(update_target)
                    desired_state = "UPDATING"
                    payload["status"] = "UPDATING"
                if control.get("self_test_requested_at") and future is None:
                    next_self_test = 0
                if pending_logs:
                    response = client.post(f"{core}/api/logs/ingest", json={"node_name": payload["node_name"],
                                                                            "entries": pending_logs[:]})
                    response.raise_for_status()
                    pending_logs.clear()
            except httpx.HTTPError:
                logger.warning("CORE request failed", extra={"node_name": payload["node_name"]})
            except Exception as error:
                payload["status"] = "ERROR"
                pending_logs.append({"level": "ERROR", "message": str(error),
                                     "job_id": payload.get("current_job")})
                logger.exception("Worker loop failed", extra={"node_name": payload["node_name"]})
                try:
                    client.post(f"{core}/api/workers/heartbeat", json=payload)
                except httpx.HTTPError:
                    pass
            time.sleep(15)

if __name__ == "__main__":
    main()
