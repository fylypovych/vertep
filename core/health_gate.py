"""Single definition of a healthy Vertep installation after an update.

The gate is used by the update agent, the CLI and the Web UI so that "the update
succeeded" always means the same thing: every Core dependency answers, every
Worker passed a recent role self-test and the job queue is back in service.
"""

import json
import os
import socket
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

CORE_ROLES = {"core", "core-worker"}
WORKER_ROLES = {"worker", "core-worker"}
POST_UPDATE = "post-update"
FINAL = "final"


@dataclass(frozen=True)
class CheckResult:
    name: str
    component: str
    passed: bool
    detail: str
    critical: bool = True


def _check(name: str, component: str, passed: bool, detail: str, critical: bool = True) -> CheckResult:
    return CheckResult(name=name, component=component, passed=passed, detail=detail, critical=critical)


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() == "true"


def _age_seconds(timestamp: str, now: datetime) -> float | None:
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment.astimezone(timezone.utc)).total_seconds()


def expected_migrations(root: Path | None = None) -> list[str]:
    directory = Path(os.getenv("MIGRATIONS_ROOT", str((root or Path(__file__).resolve().parents[1]) / "db")))
    return sorted(path.name for path in directory.glob("[0-9][0-9][0-9]_*.sql"))


def evaluate_core(snapshot: dict, expected_version: str, mode: str = POST_UPDATE) -> list[CheckResult]:
    """Assert every Core dependency named in the safe update specification."""
    results = []
    postgres = snapshot.get("postgres") or {}
    missing = sorted(set(postgres.get("expected_migrations") or []) - set(postgres.get("applied_migrations") or []))
    results.append(_check("postgres", "core", bool(postgres.get("reachable")) and not missing,
                          postgres.get("error") or (f"pending migrations: {', '.join(missing)}" if missing
                                                    else "reachable, schema up to date")))
    redis = snapshot.get("redis") or {}
    results.append(_check("redis", "core", bool(redis.get("roundtrip")),
                          redis.get("error") or f"backend={redis.get('backend', 'unknown')}, read-write verified"))
    api = snapshot.get("api") or {}
    reported = str(api.get("version") or "")
    results.append(_check("api", "core", api.get("status") == "ok" and reported == expected_version,
                          api.get("error") or f"status={api.get('status')}, version={reported or 'unknown'}, "
                                              f"expected={expected_version}"))
    web = snapshot.get("web_ui") or {}
    results.append(_check("web_ui", "core", web.get("status_code") == 200,
                          web.get("error") or f"HTTP {web.get('status_code')}"))
    ollama = snapshot.get("ollama") or {}
    results.append(_check("ollama", "core", not ollama.get("required") or bool(ollama.get("reachable")),
                          ollama.get("error") or ("reachable" if ollama.get("reachable") else "not configured"),
                          critical=bool(ollama.get("required"))))
    dispatcher = snapshot.get("dispatcher") or {}
    registered = int(dispatcher.get("registered_workers") or 0)
    eligible = int(dispatcher.get("eligible_workers") or 0)
    results.append(_check("dispatcher", "core", registered == 0 or eligible > 0,
                          f"{eligible}/{registered} registered workers can accept work"))
    return results


def evaluate_worker(snapshot: dict, expected_version: str, now: datetime | None = None) -> list[CheckResult]:
    """Assert the local Worker runtime, its accelerator and its role self-test."""
    now = now or datetime.now(timezone.utc)
    results = []
    required = bool(snapshot.get("gpu_required"))
    available = bool(snapshot.get("gpu_available"))
    results.append(_check("gpu", "worker", available or not required,
                          f"{snapshot.get('gpu_name', 'unknown')} (required={required})"))
    driver = str(snapshot.get("driver_version") or "")
    results.append(_check("driver", "worker", bool(driver) and driver != "unknown" or not required,
                          f"driver={driver or 'unknown'}"))
    cuda = str(snapshot.get("cuda_version") or "")
    results.append(_check("cuda", "worker", bool(cuda) and cuda != "unknown" or not required,
                          f"cuda={cuda or 'unknown'}"))
    minimum_vram = int(snapshot.get("min_vram_mb") or 0)
    free_vram = int(snapshot.get("free_vram_mb") or snapshot.get("vram_mb") or 0)
    results.append(_check("vram", "worker", free_vram >= minimum_vram or not required,
                          f"free {free_vram} MB, required {minimum_vram} MB"))
    runtime = snapshot.get("comfyui") or {}
    results.append(_check("comfyui", "worker", not runtime.get("required") or bool(runtime.get("reachable")),
                          runtime.get("error") or ("reachable" if runtime.get("reachable") else "not required"),
                          critical=bool(runtime.get("required"))))
    api = snapshot.get("worker_api") or {}
    results.append(_check("worker_api", "worker", bool(api.get("reachable", True)),
                          api.get("error") or "worker process is serving"))
    self_test = snapshot.get("self_test") or {}
    age = _age_seconds(self_test.get("checked_at", ""), now)
    maximum_age = int(os.getenv("WORKER_SELF_TEST_MAX_AGE_SECONDS", "900"))
    fresh = age is not None and -int(os.getenv("WORKER_CLOCK_SKEW_SECONDS", "300")) <= age <= maximum_age
    results.append(_check("self_test", "worker", self_test.get("status") == "PASSED" and fresh,
                          f"status={self_test.get('status', 'MISSING')}, "
                          f"age={'unknown' if age is None else int(age)}s, limit={maximum_age}s"))
    version = str(snapshot.get("version") or "")
    results.append(_check("worker_version", "worker", version == expected_version,
                          f"running {version or 'unknown'}, expected {expected_version}"))
    return results


def evaluate_fleet(snapshot: dict, expected_version: str, mode: str = POST_UPDATE,
                   now: datetime | None = None) -> list[CheckResult]:
    """Assert that every registered node is READY and that the queue is back in service."""
    now = now or datetime.now(timezone.utc)
    results = []
    nodes = [node for node in snapshot.get("nodes") or [] if not node.get("revoked_at")]
    runtimes = {worker.get("node_name"): worker for worker in snapshot.get("workers") or []}
    unhealthy = []
    for node in nodes:
        runtime = runtimes.get(node.get("node_id"))
        status = (runtime or {}).get("status", "OFFLINE")
        if status != "READY":
            unhealthy.append(f"{node.get('node_id')}={status}")
    allow_offline = _flag("HEALTH_GATE_ALLOW_OFFLINE_NODES")
    results.append(_check("workers_ready", "fleet", not unhealthy or allow_offline,
                          f"{len(nodes) - len(unhealthy)}/{len(nodes)} nodes READY"
                          + (f"; not ready: {', '.join(unhealthy)}" if unhealthy else ""),
                          critical=not allow_offline))
    stale = sorted(name for name, worker in runtimes.items()
                   if str(worker.get("version") or "") not in {expected_version, ""})
    require_fleet_version = _flag("HEALTH_GATE_REQUIRE_FLEET_VERSION")
    results.append(_check("fleet_version", "fleet", not stale,
                          f"nodes on another version: {', '.join(stale)}" if stale
                          else f"all nodes report {expected_version}",
                          critical=require_fleet_version))
    queue = snapshot.get("queue") or {}
    inflight = int(queue.get("inflight") or 0)
    results.append(_check("queue_drained", "fleet", inflight == 0, f"{inflight} leased tasks in flight"))
    if mode == FINAL:
        paused = bool(queue.get("paused"))
        results.append(_check("queue_resumed", "fleet", not paused,
                              "dispatch is paused" if paused else "dispatch resumed"))
        waiting = int(queue.get("waiting_for_system") or 0)
        results.append(_check("jobs_released", "fleet", waiting == 0,
                              f"{waiting} jobs still WAITING_FOR_SYSTEM"))
        state = str(snapshot.get("system_state") or "")
        results.append(_check("system_state", "fleet", state == "NORMAL", f"system state is {state or 'unknown'}"))
    return results


def gate_report(role: str, snapshot: dict, expected_version: str, mode: str = POST_UPDATE,
                now: datetime | None = None) -> dict:
    """Combine the Core, Worker and fleet checks that define a successful update."""
    if mode not in {POST_UPDATE, FINAL}:
        raise ValueError(f"Unknown health gate mode: {mode}")
    results: list[CheckResult] = []
    if role in CORE_ROLES:
        results += evaluate_core(snapshot, expected_version, mode)
        results += evaluate_fleet(snapshot, expected_version, mode, now)
    if role in WORKER_ROLES:
        results += evaluate_worker(snapshot.get("worker") or snapshot, expected_version, now)
    if not results:
        raise ValueError(f"Unknown node role: {role}")
    failed = [item for item in results if not item.passed and item.critical]
    return {"passed": not failed, "role": role, "mode": mode, "version": expected_version,
            "checked_at": (now or datetime.now(timezone.utc)).isoformat(),
            "failed": [item.name for item in failed],
            "checks": [asdict(item) for item in results]}


def probe_http(url: str, timeout: float = 5.0, token: str | None = None) -> dict:
    request = Request(url)
    if token:
        request.add_header("Authorization", token)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(65536)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            return {"reachable": True, "status_code": response.status, "payload": payload}
    except Exception as error:
        return {"reachable": False, "status_code": None, "error": f"{url}: {error}"}


def probe_postgres(root: Path | None = None) -> dict:
    dsn = os.getenv("DATABASE_URL", "")
    # Installations that do not use PostgreSQL have no schema to verify; every
    # other configuration must prove the migration journal, not just an open port.
    expected = expected_migrations(root) if os.getenv("STORAGE_BACKEND", "file").lower() == "postgres" else []
    if not dsn:
        if not expected:
            return {"reachable": True, "applied_migrations": [], "expected_migrations": [],
                    "error": ""}
        host, port = os.getenv("POSTGRES_HOST", "postgres"), int(os.getenv("POSTGRES_PORT", "5432"))
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError as error:
            return {"reachable": False, "applied_migrations": [], "expected_migrations": expected,
                    "error": f"{host}:{port}: {error}"}
        return {"reachable": False, "applied_migrations": [], "expected_migrations": expected,
                "error": "DATABASE_URL is not configured, so the migration journal cannot be verified"}
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SELECT 1")
            applied = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        return {"reachable": True, "applied_migrations": sorted(applied), "expected_migrations": expected,
                "error": ""}
    except Exception as error:
        return {"reachable": False, "applied_migrations": [], "expected_migrations": expected, "error": str(error)}


def probe_redis() -> dict:
    try:
        import redis
        client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True,
                                      socket_connect_timeout=3)
        client.ping()
        key = "vertep:health-gate"
        client.set(key, "ok", ex=30)
        value = client.get(key)
        client.delete(key)
        return {"backend": "redis", "roundtrip": value == "ok", "error": "" if value == "ok"
                else "Redis did not return the value written by the health gate"}
    except Exception as error:
        return {"backend": "unavailable", "roundtrip": False, "error": str(error)}


def probe_ollama() -> dict:
    url = os.getenv("OLLAMA_URL", "")
    if not url:
        return {"required": False, "reachable": False, "error": ""}
    probe = probe_http(url.rstrip("/") + "/api/tags", timeout=float(os.getenv("HEALTH_GATE_TIMEOUT", "5")))
    return {"required": True, "reachable": bool(probe.get("reachable")), "error": probe.get("error", "")}
