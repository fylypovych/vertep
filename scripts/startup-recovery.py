#!/usr/bin/env python3
"""Startup recovery: restore system state after power loss/crash."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Host unit: CORE sources live next to scripts/, not on the default import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], **kwargs) -> str:
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        return f"ERROR: {error}"


def get_root() -> Path:
    return Path(os.getenv("VERTEP_ROOT", Path(__file__).resolve().parent.parent))


def get_update_state_dir() -> Path:
    from core.first_run import default_update_state_dir
    return default_update_state_dir()


def recover_interrupted_update(root: Path, state_dir: Path) -> bool:
    """Return True when a release was rolled back and must still prove its health."""
    status_file = state_dir / "status.json"
    if not status_file.exists():
        return False
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    phase = status.get("phase")
    if status.get("state") != "RUNNING" or phase not in {"UPDATING", "RESTARTING", "VERIFYING", "RECOVERING"}:
        return False
    print(f"{now()} STARTUP-RECOVERY: interrupted update detected, rolling back", file=sys.stderr)
    rollback_script = root / "scripts" / "vertep"
    output = run(["/bin/bash", str(rollback_script), "rollback"], cwd=root)
    print(f"{now()} STARTUP-RECOVERY: rollback result: {output}", file=sys.stderr)
    if output.startswith("ERROR"):
        status.update({"state": "FAILED", "phase": "EMERGENCY", "message": output})
        (state_dir / "system-state.json").write_text(json.dumps({
            "state": "EMERGENCY", "updated_at": now(),
            "reason": "Startup rollback failed", "operation_id": status.get("request_id")
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        status.update({"state": "ROLLED_BACK", "phase": "NORMAL",
                       "message": "Interrupted update rolled back"})
    status["updated_at"] = now()
    status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return not output.startswith("ERROR")


def restore_system_state(root: Path, state_dir: Path) -> None:
    system_state_file = state_dir / "system-state.json"
    if not system_state_file.exists():
        return
    try:
        state = json.loads(system_state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    current_state = state.get("state", "NORMAL")
    if str(state.get("operation_id") or "").startswith("restore:"):
        print(f"{now()} STARTUP-RECOVERY: restore in progress, keeping {current_state}", file=sys.stderr)
        return
    if current_state in {"MAINTENANCE", "UPDATING", "RECOVERING"}:
        print(f"{now()} STARTUP-RECOVERY: restoring system state from {current_state} to NORMAL", file=sys.stderr)
        state["state"] = "NORMAL"
        state["updated_at"] = now()
        state["reason"] = "Startup recovery"
        system_state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compose_command(root: Path) -> list[str]:
    """Same selection as scripts/vertep: the active release owns the compose files."""
    release = root / "current" if (root / "current").is_symlink() else root
    compose = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(release / "docker-compose.yml")]
    try:
        environment = (root / ".env").read_text(encoding="utf-8")
    except OSError:
        environment = ""
    if "GPU_VENDOR=amd" in environment and (release / "docker-compose.amd.yml").is_file():
        compose.extend(["-f", str(release / "docker-compose.amd.yml")])
    if "GPU_VENDOR=nvidia" in environment and (release / "docker-compose.nvidia.yml").is_file():
        compose.extend(["-f", str(release / "docker-compose.nvidia.yml")])
    return compose


def selected_services(root: Path) -> list[str]:
    """Return non-oneshot Compose services selected by the deployment plan."""
    plan_path = root / "config" / "deployment-plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    if isinstance(plan, dict) and isinstance(plan.get("services"), list):
        services = sorted({
            str(service).strip()
            for service in plan["services"]
            if isinstance(service, str) and service.strip() and service.strip() != "migrate"
        })
        if services:
            return services
    role = (os.getenv("ROLE") or os.getenv("NODE_ROLE") or "").strip().lower()
    if not role:
        try:
            for line in (root / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith(("ROLE=", "NODE_ROLE=")):
                    role = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                    if role:
                        break
        except OSError:
            pass
    if role == "worker":
        return ["worker"]
    return []


def parse_compose_ps(output: str) -> list[dict]:
    """Parse both NDJSON (Compose v2.21+) and JSON array output from compose ps."""
    text = (output or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        except ValueError:
            return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def inspect_compose_services(compose: list[str], runner=subprocess.run,
                             timeout: float | None = None) -> list[dict]:
    inspect_timeout = (timeout if timeout is not None
                       else float(os.getenv("STARTUP_RECOVERY_INSPECT_TIMEOUT", "30")))
    result = runner(
        [*compose, "ps", "--all", "--format", "json", "--no-trunc"],
        check=True, capture_output=True, text=True, timeout=inspect_timeout,
    )
    return parse_compose_ps(getattr(result, "stdout", "") or "")


def _index_rows(rows: list[dict]) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for row in rows:
        service = row.get("Service")
        if not isinstance(service, str) or not service:
            continue
        existing = indexed.get(service)
        # Prefer a running container record when a stale exited replacement exists.
        if existing is None or str(row.get("State") or "").lower() == "running":
            indexed[service] = row
    return indexed


def _classify_services(required: list[str], rows: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Split required services into (ready, starting, needs_start)."""
    by_service = _index_rows(rows)
    ready: list[str] = []
    starting: list[str] = []
    needs_start: list[str] = []
    for service in required:
        row = by_service.get(service)
        if row is None:
            needs_start.append(service)
            continue
        state = str(row.get("State") or "").lower()
        health = str(row.get("Health") or "").lower()
        if state == "running" and health in {"", "healthy"}:
            ready.append(service)
        elif state == "running" and health == "starting":
            starting.append(service)
        else:
            needs_start.append(service)
    return ready, starting, needs_start


def _format_service_states(services: list[str], rows: list[dict]) -> str:
    by_service = _index_rows(rows)
    details = []
    for service in services:
        row = by_service.get(service)
        if row is None:
            details.append(f"{service}=missing")
            continue
        state = str(row.get("State") or "unknown").lower()
        health = str(row.get("Health") or "").lower()
        details.append(f"{service}={state}/{health}" if health else f"{service}={state}")
    return ", ".join(details)


def wait_for_services_ready(compose: list[str], required: list[str], runner=subprocess.run,
                            timeout_seconds: float | None = None,
                            poll_seconds: float | None = None,
                            stop_on_failure: bool = False) -> tuple[bool, str, list[str]]:
    """Wait bounded time for required services to be running and healthy."""
    timeout = (timeout_seconds if timeout_seconds is not None
               else float(os.getenv("STARTUP_RECOVERY_READY_TIMEOUT", "120")))
    poll = (poll_seconds if poll_seconds is not None
            else float(os.getenv("STARTUP_RECOVERY_POLL_SECONDS", "5")))
    deadline = time.monotonic() + max(0.0, timeout)
    last_rows: list[dict] = []
    while True:
        last_rows = inspect_compose_services(compose, runner=runner)
        ready, starting, needs_start = _classify_services(required, last_rows)
        if not starting and not needs_start:
            return True, f"all {len(ready)} services running and healthy", []
        if stop_on_failure and needs_start:
            return False, _format_service_states(needs_start, last_rows), needs_start
        if time.monotonic() >= deadline:
            unready = needs_start + starting
            return False, f"timed out waiting for readiness ({_format_service_states(unready, last_rows)})", unready
        time.sleep(max(0.0, min(poll, deadline - time.monotonic())))


def _run_compose_up(compose: list[str], services: list[str] | None, runner, timeout: float) -> str | None:
    """Start only requested services (or full stack if None); return error string or None."""
    cmd = [*compose, "up", "-d", *services] if services else [*compose, "up", "-d", "--remove-orphans"]
    try:
        runner(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        return None
    except subprocess.TimeoutExpired:
        target = ", ".join(services) if services else "all services"
        return f"ERROR: docker compose up ({target}) timed out after {int(timeout)} seconds"
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()[-500:]
        return f"ERROR: docker compose up failed (exit {error.returncode}): {detail}"
    except FileNotFoundError as error:
        return f"ERROR: {error}"


def restart_containers(root: Path, runner=subprocess.run,
                       ready_timeout: float | None = None,
                       poll_seconds: float | None = None,
                       up_timeout: float | None = None) -> str:
    compose = compose_command(root)
    effective_up_timeout = (up_timeout if up_timeout is not None
                            else float(os.getenv("STARTUP_RECOVERY_UP_TIMEOUT", "180")))
    try:
        rows = inspect_compose_services(compose, runner=runner)
    except subprocess.TimeoutExpired as error:
        return f"ERROR: docker compose ps timed out after {error.timeout} seconds"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as error:
        return f"ERROR: failed to inspect running stack: {error}"

    required = selected_services(root)
    if not required:
        non_migrate = [row for row in rows
                       if isinstance(row.get("Service"), str) and row.get("Service") not in {"", "migrate"}]
        if non_migrate:
            required = sorted({row["Service"] for row in non_migrate})
        else:
            err = _run_compose_up(compose, None, runner, effective_up_timeout)
            return err if err else "containers started"

    ready, starting, needs_start = _classify_services(required, rows)
    if not needs_start:
        if not starting:
            return f"stack already running and healthy ({len(ready)} services)"
        try:
            ok, detail, failed = wait_for_services_ready(
                compose, required, runner=runner,
                timeout_seconds=ready_timeout, poll_seconds=poll_seconds,
                stop_on_failure=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as error:
            return f"ERROR: readiness check failed: {error}"
        if ok:
            return f"stack already running and healthy ({len(required)} services)"
        # Only re-issue compose up if a starting container actually exited/became unhealthy;
        # a genuine readiness timeout reports an error directly.
        _, _, failed_now = _classify_services(failed, inspect_compose_services(compose, runner=runner))
        if not failed_now:
            return f"ERROR: {detail}"
        needs_start = failed_now

    err = _run_compose_up(compose, needs_start, runner, effective_up_timeout)
    if err:
        return err

    try:
        ok, detail, _ = wait_for_services_ready(
            compose, required, runner=runner,
            timeout_seconds=ready_timeout, poll_seconds=poll_seconds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as error:
        return f"ERROR: post-recovery readiness check failed: {error}"
    if not ok:
        return f"ERROR: recovered services ({', '.join(needs_start)}) not ready: {detail}"
    return f"recovered services: {', '.join(needs_start)}"


def core_is_healthy(attempts: int = 60, delay: float = 5) -> bool:
    # CORE_ADDRESS is the address inside the Docker network, not reachable from the host.
    core = os.getenv("VERTEP_CORE_URL") or os.getenv("CORE_URL") or "https://127.0.0.1:8443"
    url = (core if core.startswith(("http://", "https://")) else f"http://{core}").rstrip("/") + "/api/health"
    for attempt in range(attempts):
        if not run(["curl", "-kfsS", "--connect-timeout", "5", url]).startswith("ERROR"):
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


def mark_emergency(state_dir: Path, reason: str) -> None:
    (state_dir / "system-state.json").write_text(json.dumps({
        "state": "EMERGENCY", "updated_at": now(), "reason": reason, "operation_id": None
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reconcile_database_state(state_dir: Path, attempts: int = 60, delay: float = 5) -> None:
    """Apply the same rules to PostgreSQL, the record CORE really reads in production.

    It needs the database container, so it runs after the stack starts. An unreachable
    database changes nothing: the fence or transitional state stays in force.
    """
    if os.getenv("SYSTEM_STATE_BACKEND", "file").lower() != "postgres":
        return
    from core.system_state import _database_state, set_system_state
    for attempt in range(attempts):
        try:
            state = _database_state()
            break
        except Exception as error:  # noqa: BLE001 - psycopg is not always importable on the host
            if attempt + 1 == attempts:
                print(f"{now()} STARTUP-RECOVERY: database state unavailable: {error}", file=sys.stderr)
                return
            time.sleep(delay)
    try:
        local = json.loads((state_dir / "system-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local = {}
    if local.get("state") == "EMERGENCY":
        set_system_state("EMERGENCY", str(local.get("reason") or "Startup recovery failed"),
                         local.get("operation_id"), state_dir)
    elif (state and not str(state.get("operation_id") or "").startswith("restore:")
            and state["state"] in {"MAINTENANCE", "UPDATING", "RECOVERING"}):
        set_system_state("NORMAL", "Startup recovery", None, state_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vertep startup recovery check")
    parser.parse_args(argv)
    root = get_root()
    state_dir = get_update_state_dir()
    print(f"{now()} STARTUP-RECOVERY: checking system state", file=sys.stderr)
    rolled_back = recover_interrupted_update(root, state_dir)
    restore_system_state(root, state_dir)
    output = restart_containers(root)
    print(f"{now()} STARTUP-RECOVERY: {output}", file=sys.stderr)
    recovery_failed = output.startswith("ERROR")
    role = os.getenv("ROLE") or os.getenv("NODE_ROLE") or ""
    on_core = not role or role == "unknown" or role.startswith("core")
    if rolled_back and on_core and (recovery_failed or not core_is_healthy()):
        mark_emergency(state_dir, "Rolled back release did not become healthy")
        reconcile_database_state(state_dir, attempts=5 if recovery_failed else 60)
        return 1
    if recovery_failed:
        return 1
    reconcile_database_state(state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
