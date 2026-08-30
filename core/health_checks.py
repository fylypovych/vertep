"""Universal health checks for all Vertep node roles."""

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def _http_get(url: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        import urllib.request
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status < 400, f"HTTP {response.status}"
    except Exception as error:
        return False, str(error)[:200]


def check_tcp(host: str, port: int, timeout: int = 5) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open"
    except Exception as error:
        return False, str(error)[:200]


def check_docker() -> tuple[bool, str]:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        return True, "ok"
    except Exception as error:
        return False, str(error)[:200]


def check_postgres(database_url: str | None = None) -> tuple[bool, str]:
    url = database_url or os.getenv("DATABASE_URL", "")
    if not url:
        return True, "skipped"
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        return True, "ok"
    except Exception as error:
        return False, str(error)[:200]


def check_redis(redis_url: str | None = None) -> tuple[bool, str]:
    url = redis_url or os.getenv("REDIS_URL", "")
    if not url:
        return True, "skipped"
    try:
        import redis
        client = redis.Redis.from_url(url, socket_timeout=5)
        client.ping()
        return True, "ok"
    except Exception as error:
        return False, str(error)[:200]


def check_core_api(core_url: str) -> tuple[bool, str]:
    if not core_url:
        return True, "skipped"
    return _http_get(core_url.rstrip("/") + "/api/health")


def check_gpu() -> tuple[bool, str]:
    try:
        if os.getenv("GPU_VENDOR") == "amd":
            output = subprocess.run(["rocminfo"], check=True, capture_output=True,
                                    text=True, timeout=20).stdout
            return True, next((line.strip() for line in output.splitlines()
                               if "Name:" in line), "AMD GPU")
        output = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                                check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        return True, output.splitlines()[0] if output else "no gpu"
    except Exception as error:
        return False, str(error)[:200]


def check_cuda() -> tuple[bool, str]:
    try:
        if os.getenv("GPU_VENDOR") == "amd":
            output = subprocess.run(["rocminfo", "--version"], capture_output=True,
                                    text=True, timeout=10).stdout.strip()
            return True, output or os.getenv("ROCM_VERSION", "ROCm")
        output = subprocess.run(["nvcc", "--version"], capture_output=True, text=True, timeout=10).stdout
        if "release" in output:
            return True, output.splitlines()[0]
        return False, "nvcc not found"
    except Exception as error:
        return False, str(error)[:200]


def check_ollama(url: str | None = None) -> tuple[bool, str]:
    target = url or os.getenv("OLLAMA_URL", "http://localhost:11434")
    return _http_get(target.rstrip("/") + "/api/tags", timeout=10)


def check_comfyui(url: str | None = None) -> tuple[bool, str]:
    target = url or os.getenv("COMFYUI_URL", "http://localhost:8188")
    return _http_get(target.rstrip("/") + "/system_stats", timeout=10)


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
    target = root or os.getenv("BACKUP_ROOT", "/data/backups")
    try:
        Path(target).mkdir(parents=True, exist_ok=True)
        return True, f"writable: {target}"
    except Exception as error:
        return False, str(error)[:200]


def check_monitoring(prometheus_url: str | None = None) -> tuple[bool, str]:
    target = prometheus_url or os.getenv("PROMETHEUS_URL", "http://localhost:9090/-/healthy")
    if not target:
        return True, "skipped"
    return _http_get(target, timeout=10)


def run_checks(role: str | None = None) -> dict:
    role = role or os.getenv("NODE_ROLE", "core")
    checks = {"role": role, "checked_at": datetime.now(timezone.utc).isoformat()}
    checks["docker"] = check_docker()
    if role == "core":
        checks["postgres"] = check_postgres()
        checks["redis"] = check_redis()
        checks["core_api"] = (True, "local")
        checks["ollama"] = check_ollama()
        checks["monitoring"] = check_monitoring()
    elif role == "gpu":
        checks["gpu"] = check_gpu()
        checks["cuda"] = check_cuda()
        checks["comfyui"] = check_comfyui()
    elif role == "text":
        checks["ollama"] = check_ollama()
    elif role == "voice":
        checks["tts"] = check_tts()
    elif role == "publisher":
        checks["publisher"] = check_publisher()
    elif role == "backup":
        checks["backup"] = check_backup()
    elif role == "monitoring":
        checks["monitoring"] = check_monitoring()
    core_url = os.getenv("CORE_ADDRESS", "")
    if core_url:
        checks["core_api"] = check_core_api(core_url)
    history_path = Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update")) / "health-history.jsonl"
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": checks["checked_at"], "role": role,
                                "status": health_status(checks), "checks": checks}, ensure_ascii=False) + "\n")
    except (OSError, ValueError):
        pass
    return checks


def health_status(checks: dict) -> str:
    failures = [name for name, value in checks.items() if isinstance(value, tuple) and value[0] is False]
    return "UNHEALTHY" if failures else "HEALTHY"
