#!/usr/bin/env python3
"""Host-level watchdog for Vertep nodes."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], **kwargs) -> str:
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        return f"ERROR: {error}"


def check_role() -> str:
    try:
        return Path(os.getenv("NODE_ROLE_FILE", "/etc/vertep/node.conf")).read_text(encoding="utf-8").split("ROLE=", 1)[1].splitlines()[0].strip()
    except (OSError, IndexError, ValueError):
        return "unknown"


def check_containers(role: str) -> dict:
    root = Path(os.getenv("VERTEP_ROOT", Path(__file__).resolve().parent.parent))
    compose = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "docker-compose.yml")]
    required = {"core": ["core", "postgres", "redis"],
                "gpu": ["worker"],
                "text": ["worker", "ollama"],
                "voice": ["worker", "tts"],
                "publisher": ["worker", "publisher-worker"],
                "backup": ["worker", "backup-service"],
                "monitoring": ["worker", "monitoring"]}.get(role, ["worker"])
    running = set(run([*compose, "ps", "--services", "--filter", "status=running"]).splitlines())
    return {svc: (svc in running) for svc in required}


def check_gpu() -> dict:
    return {"nvidia_smi": run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).startswith("ERROR") is False}


def check_core_api(core_url: str) -> dict:
    url = core_url.rstrip("/") + "/api/health"
    result = run(["curl", "-kfsS", "--connect-timeout", "5", url])
    return {"core_api": not result.startswith("ERROR")}


def check_update_agent() -> dict:
    state_dir = Path(os.getenv("UPDATE_STATE_DIR", "/var/lib/vertep/update"))
    status_file = state_dir / "status.json"
    if not status_file.exists():
        return {"update_agent": False}
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        return {"update_agent": status.get("state") not in {"FAILED", "ROLLED_BACK"}}
    except (OSError, ValueError):
        return {"update_agent": False}


def restart_containers(role: str) -> str:
    root = Path(os.getenv("VERTEP_ROOT", Path(__file__).resolve().parent.parent))
    compose = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "docker-compose.yml")]
    services = {"core": ["core", "postgres", "redis"],
                "gpu": ["worker"],
                "text": ["worker", "ollama"],
                "voice": ["worker", "tts"],
                "publisher": ["worker", "publisher-worker"],
                "backup": ["worker", "backup-service"],
                "monitoring": ["worker", "monitoring"]}.get(role, ["worker"])
    output = run([*compose, "restart", *services], timeout=120)
    return output


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
    return checks


def health_status(checks: dict) -> str:
    failures = [name for name, value in checks.items() if isinstance(value, tuple) and value[0] is False]
    return "UNHEALTHY" if failures else "HEALTHY"


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


def report_to_core(checks: dict) -> str:
    core_url = os.getenv("CORE_ADDRESS", "")
    if not core_url:
        return "skipped: no CORE_ADDRESS"
    url = core_url.rstrip("/") + "/api/watchdog/report"
    payload = json.dumps({"role": checks.get("role"), "checks": {key: value for key, value in checks.items() if isinstance(value, tuple)},
                          "restarted": checks.get("restarted", False), "timestamp": now()})
    try:
        import urllib.request
        request = urllib.request.Request(url, data=payload.encode("utf-8"),
                                        headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return f"reported: HTTP {response.status}"
    except Exception as error:
        return f"report failed: {error}"


def main() -> None:
    role = check_role()
    root = Path(os.getenv("VERTEP_ROOT", Path(__file__).resolve().parent.parent))
    core_url = os.getenv("CORE_ADDRESS", os.getenv("VERTEP_CORE_URL", "http://localhost:8080"))
    checks = {"role": role, "checked_at": now()}
    checks.update(check_containers(role))
    if role == "gpu":
        checks.update(check_gpu())
    if role != "core":
        checks.update(check_core_api(core_url))
    checks.update(check_update_agent())
    failed = [name for name, ok in checks.items() if ok is False]
    if failed:
        print(f"{now()} WATCHDOG: restarting {', '.join(failed)}", file=sys.stderr)
        restart_output = restart_containers(role)
        checks["restart_output"] = restart_output
        print(f"{now()} WATCHDOG: restart result: {restart_output}", file=sys.stderr)
    checks["restarted"] = bool(failed)
    state = {"last_check": now(), "checks": checks, "restarted": bool(failed)}
    state_path = root / "config" / "watchdog-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if role != "core":
        report_result = report_to_core(checks)
        print(f"{now()} WATCHDOG: {report_result}", file=sys.stderr)


if __name__ == "__main__":
    main()
