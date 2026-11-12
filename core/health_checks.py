"""Universal health checks for all Vertep node roles."""

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_HEALTH_CHECKS = {
    "core": frozenset({"docker", "postgres", "redis", "core_api"}),
    "gpu": frozenset({"docker", "gpu", "cuda", "comfyui"}),
    "text": frozenset({"docker", "ollama"}),
    "voice": frozenset({"docker", "tts"}),
    "publisher": frozenset({"docker", "publisher"}),
    "backup": frozenset({"docker", "backup"}),
    "monitoring": frozenset({"docker", "monitoring"}),
}


def health_selection(role: str, backend: str | None = None,
                     additional_roles: list[str] | None = None) -> tuple[set[str], set[str]]:
    """Use the same applied plan and installation backend as deployment.

    An explicit wizard backend replaces the saved backend. Explicit additional
    roles replace the saved extras, rather than accumulating stale choices.
    """
    from .first_run import config_root
    def document(name):
        try:
            value = json.loads((config_root() / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    plan = document("deployment-plan.json")
    installation = document("installation.json")
    selection = {} if installation.get("completed_at") else document("setup-selection.json")
    if additional_roles is None:
        extras = (set(plan.get("additional_roles", [])) if plan.get("role") == role else
                  {item.strip() for item in os.getenv("NODE_ADDITIONAL_ROLES", "").split(",") if item.strip()})
        if selection.get("role") == role:
            extras = set(selection.get("additional_roles", []))
    else:
        extras = set(additional_roles)
    if role != "core":
        extras = set()
    roles = {role, *extras}
    if not roles <= REQUIRED_HEALTH_CHECKS.keys():
        raise ValueError("Unsupported health role")
    saved = installation.get("ai_backend") or {}
    backend = backend if backend is not None else saved.get("type", selection.get("ai_backend", "skip"))
    if backend not in {"skip", "ollama", "external", "openai"}:
        raise ValueError("Unsupported AI backend")
    required = set().union(*(REQUIRED_HEALTH_CHECKS[item] for item in roles))
    if role == "core" and backend == "ollama":
        required.add("ollama")
    return roles, required


def _http_get(url: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        import urllib.request
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status < 400, f"HTTP {response.status}"
    except Exception:
        return False, "health_endpoint_unavailable"


def check_tcp(host: str, port: int, timeout: int = 5) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open"
    except Exception:
        return False, "health_connection_unavailable"


def check_docker() -> tuple[bool, str]:
    path = os.getenv("HOST_HEALTH_OBSERVATION")
    if path:
        from .health_observation import read_observation
        return read_observation(Path(path), "docker")
    if Path('/.dockerenv').exists() or Path('/run/.containerenv').exists():
        return False, "health_observation_unavailable"
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        return True, "ok"
    except Exception as error:
        return False, "health_docker_unavailable"


def check_postgres(database_url: str | None = None) -> tuple[bool | None, str]:
    url = database_url or os.getenv("DATABASE_URL", "")
    if not url:
        return None, "not-applicable: DATABASE_URL is not configured"
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    return False, "SELECT 1 did not return 1"
        return True, "SELECT 1 = 1"
    except Exception:
        return False, "health_postgres_unavailable"


def check_redis(redis_url: str | None = None) -> tuple[bool | None, str]:
    url = redis_url or os.getenv("REDIS_URL", "")
    if not url:
        return None, "not-applicable: REDIS_URL is not configured"
    try:
        import redis
        client = redis.Redis.from_url(url, socket_timeout=5)
        if client.ping() is not True:
            return False, "Redis did not acknowledge PING"
        return True, "PING = PONG"
    except Exception:
        return False, "health_redis_unavailable"


def check_core_api(core_url: str) -> tuple[bool | None, str]:
    if not core_url:
        return None, "not-applicable: CORE_ADDRESS is not configured"
    return _http_get(core_url.rstrip("/") + "/api/health/ready")


def check_gpu() -> tuple[bool | None, str]:
    if os.getenv("HOST_HEALTH_OBSERVATION"):
        from .health_observation import read_observation
        return read_observation(Path(os.environ["HOST_HEALTH_OBSERVATION"]), "gpu")
    try:
        if os.getenv("GPU_VENDOR") == "amd":
            output = subprocess.run(["rocminfo"], check=True, capture_output=True,
                                    text=True, timeout=20).stdout
            name = next((line.strip() for line in output.splitlines() if "Name:" in line), "")
            return (True, name) if name else (None, "not-applicable: no GPU detected")
        output = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                                check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        if not output:
            return None, "not-applicable: no GPU detected"
        return True, output.splitlines()[0]
    except Exception as error:
        return False, str(error)[:200]


def check_cuda() -> tuple[bool, str]:
    if os.getenv("HOST_HEALTH_OBSERVATION"):
        from .health_observation import read_observation
        return read_observation(Path(os.environ["HOST_HEALTH_OBSERVATION"]), "cuda")
    try:
        import torch
        if not torch.cuda.is_available():
            return False, "health_gpu_unavailable"
        a = torch.ones((16, 16), device="cuda")
        result = a @ a
        torch.cuda.synchronize()
        return bool((result == 16).all().item()), "health_gpu_compute"
    except Exception:
        return False, "health_gpu_runtime_failed"


def check_ollama(url: str | None = None) -> tuple[bool, str]:
    target = url or os.getenv("OLLAMA_URL", "http://localhost:11434")
    return _http_get(target.rstrip("/") + "/api/tags", timeout=10)


def check_comfyui(url: str | None = None) -> tuple[bool, str]:
    target = url or os.getenv("COMFYUI_URL", "http://localhost:8188")
    return _http_get(target.rstrip("/") + "/system_stats", timeout=10)


def check_gpu_worker() -> tuple[bool, str]:
    # Registration, heartbeat, revocation and measured self-test remain distinct
    # from the device/runtime observation and the ComfyUI HTTP probe.
    try:
        from .state import store
        from .dispatcher import current_tested_capabilities
        from .node_registry import registered_nodes
        from .role_runtime import _heartbeat_fresh
        name = os.getenv("NODE_NAME", "local-worker")
        worker = next((item for item in store.load_workers() if item.get("node_name") == name), None)
        if not worker or worker.get("role") not in {"gpu", "core"}:
            return False, "health_worker_not_ready"
        record = next((item for item in registered_nodes() if item.get("node_id") == name), None)
        if (record and (record.get("revoked_at") or record.get("status") == "REVOKED")) or (os.getenv("NODE_MTLS_REQUIRED", "false").lower() == "true" and not record):
            return False, "health_worker_not_ready"
        now = datetime.now(timezone.utc)
        evidence = (worker.get("self_test") or {}).get("evidence") or {}
        gpu_evidence = evidence.get("gpu") if worker.get("role") == "core" else evidence
        ready = (worker.get("status") in {"READY", "ONLINE", "FREE", "BUSY"}
                 and worker.get("desired_state") in {None, ""}
                 and _heartbeat_fresh(worker, now, int(os.getenv("HEARTBEAT_TIMEOUT", "45")))
                 and "image_generation" in current_tested_capabilities(worker, now)
                 and isinstance(gpu_evidence, dict) and gpu_evidence.get("kind") == "image"
                 and isinstance(gpu_evidence.get("bytes"), int) and gpu_evidence["bytes"] >= 16
                 and isinstance(gpu_evidence.get("sha256"), str) and len(gpu_evidence["sha256"]) == 64)
        return bool(ready), "health_worker_ready" if ready else "health_worker_not_ready"
    except Exception:
        return False, "health_worker_not_ready"


def check_tts(url: str | None = None) -> tuple[bool, str]:
    target = url or os.getenv("TTS_HEALTH_URL", "http://localhost:8090/health")
    if not target:
        return True, "skipped"
    return _http_get(target, timeout=10)


def check_publisher(url: str | None = None) -> tuple[bool, str]:
    target = url or os.getenv("PUBLISHER_HEALTH_URL", "http://localhost:8091/health")
    if not target:
        return True, "skipped"
    return _http_get(target, timeout=10)


def check_backup(root: str | None = None) -> tuple[bool, str]:
    target = root or os.getenv("BACKUP_ROOT", str(_PROJECT_ROOT / "backups"))
    try:
        Path(target).mkdir(parents=True, exist_ok=True)
        return True, f"writable: {target}"
    except Exception as error:
        return False, str(error)[:200]


def check_monitoring(prometheus_url: str | None = None) -> tuple[bool | None, str]:
    target = prometheus_url or os.getenv("PROMETHEUS_URL", "")
    if not target:
        return None, "not-applicable: PROMETHEUS_URL is not configured"
    return _http_get(target, timeout=10)


def effective_health_role() -> str:
    from .first_run import _read
    installation = _read("installation.json")
    role = (installation.get("node_role") if installation.get("completed_at") else
            _read("setup-selection.json").get("role")) or os.getenv("NODE_ROLE", "core")
    return "core" if role == "unassigned" else role


def run_checks(role: str | None = None, *, backend: str | None = None,
               additional_roles: list[str] | None = None, backend_url: str | None = None) -> dict:
    role = role or effective_health_role()
    if role == "unassigned":
        role = "core"
    roles, required = health_selection(role, backend, additional_roles)
    checks = {"role": role, "selected_roles": sorted(roles), "checked_at": datetime.now(timezone.utc).isoformat(),
              "required": sorted(required)}
    checks["docker"] = check_docker()
    if role == "core":
        checks["postgres"] = check_postgres()
        checks["redis"] = check_redis()
        checks["core_api"] = (True, "local")
        checks["ollama"] = (None, "not-applicable")
        checks["monitoring"] = (None, "not-applicable")
    if "gpu" in roles:
        checks["gpu"] = check_gpu()
        checks["cuda"] = check_cuda()
        checks["comfyui"] = check_comfyui()
        from .first_run import is_configured
        if role == "core" and is_configured():
            required.add("gpu_worker")
            checks["required"] = sorted(required)
            checks["gpu_worker"] = check_gpu_worker()
        else:
            checks["gpu_worker"] = (None, "health_worker_pending_enrollment")
    if "ollama" in required:
        if backend_url is None:
            from .first_run import _read
            installation = _read("installation.json")
            selected_backend = installation.get("ai_backend") or {}
            pending = {} if installation.get("completed_at") else _read("setup-selection.json")
            provider = backend if backend is not None else selected_backend.get("type", pending.get("ai_backend"))
            if provider == "ollama":
                backend_url = selected_backend.get("url") or pending.get("backend_url")
        checks["ollama"] = check_ollama(backend_url)
    if "voice" in roles:
        checks["tts"] = check_tts()
    if "publisher" in roles:
        checks["publisher"] = check_publisher()
    if "backup" in roles:
        checks["backup"] = check_backup()
    if "monitoring" in roles:
        checks["monitoring"] = check_monitoring()
    core_url = os.getenv("CORE_ADDRESS", "")
    if core_url:
        checks["core_api"] = check_core_api(core_url)
    from .first_run import default_update_state_dir
    from .health_history import record as record_history
    try:
        # Only unhealthy events are kept; the call also performs the daily rotation.
        record_history(default_update_state_dir(), {"timestamp": checks["checked_at"], "role": role,
                                                    "status": health_status(checks), "checks": checks})
    except (OSError, ValueError):
        pass
    return checks


def run_control_checks() -> dict:
    """CORE management must remain reachable to repair unavailable role services."""
    return {"role": "core", "scope": "control",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "required": sorted(REQUIRED_HEALTH_CHECKS["core"]),
            "docker": check_docker(), "postgres": check_postgres(),
            "redis": check_redis(), "core_api": (True, "local")}


def health_status(checks: dict) -> str:
    required = checks.get("required", REQUIRED_HEALTH_CHECKS.get(checks.get("role")))
    if required is not None:
        return "HEALTHY" if all(isinstance(checks.get(name), (tuple, list)) and len(checks[name]) == 2 and checks[name][0] is True
            for name in required) else "UNHEALTHY"
    failures = [name for name, value in checks.items() if isinstance(value, (tuple, list)) and len(value) == 2 and value[0] is False]
    return "UNHEALTHY" if failures else "HEALTHY"
