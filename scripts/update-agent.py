#!/usr/bin/env python3
"""Privileged, crash-recoverable agent for signed Vertep updates."""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run(command: list[str], root: Path) -> str:
    try:
        result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True,
                                timeout=int(os.getenv("UPDATE_TIMEOUT_SECONDS", "3600")))
    except subprocess.CalledProcessError as error:
        detail = ((error.stdout or "") + (error.stderr or "")).strip()[-4000:]
        raise RuntimeError(detail or f"Command failed with exit code {error.returncode}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Command timed out after {error.timeout} seconds") from error
    return (result.stdout + result.stderr).strip()


def core_json(path: str) -> dict:
    url = os.getenv("VERTEP_CORE_URL", "http://127.0.0.1:8080").rstrip("/") + path
    request = Request(url)
    password = os.getenv("ADMIN_PASSWORD")
    if password:
        credentials = f"{os.getenv('ADMIN_USER', 'admin')}:{password}"
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def transition(state_dir: Path, state: dict, phase: str, message: str) -> None:
    from core.system_state import SystemState, set_system_state
    state.update({"phase": phase, "message": message, "updated_at": now()})
    state.setdefault("log", []).append(f"{now()} {message}")
    state["log"] = state["log"][-500:]
    atomic_json(state_dir / "status.json", state)
    if phase in SystemState.__members__:
        set_system_state(SystemState[phase], message, state.get("request_id"))


def wait_for_drain(state_dir: Path, state: dict) -> None:
    deadline = time.monotonic() + int(os.getenv("UPDATE_DRAIN_TIMEOUT_SECONDS", "86400"))
    transition(state_dir, state, "MAINTENANCE", "Maintenance mode; waiting for active jobs")
    while time.monotonic() < deadline:
        readiness = core_json("/api/system/update/readiness")
        if readiness.get("ready"):
            state["readiness"] = readiness
            transition(state_dir, state, "MAINTENANCE", "Workers drained and queue paused")
            return
        time.sleep(float(os.getenv("UPDATE_DRAIN_POLL_SECONDS", "5")))
    raise RuntimeError("Timed out waiting for jobs and workers to drain")


def recover_if_interrupted(root: Path, state_dir: Path) -> None:
    """Rollback an apply interrupted by power loss; maintenance itself is safe to resume."""
    status_path = state_dir / "status.json"
    try:
        state = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if state.get("state") != "RUNNING" or state.get("phase") not in {"UPDATING", "RECOVERING"}:
        return
    transition(state_dir, state, "RECOVERING", "Interrupted update detected; restoring last good release")
    output = run(["/bin/bash", str(root / "scripts" / "vertep"), "rollback"], root)
    state["log"].extend(output.splitlines()[-100:])
    state.update({"state": "ROLLED_BACK", "phase": "NORMAL", "message": "Rollback completed",
                  "updated_at": now()})
    atomic_json(status_path, state)
    from core.system_state import SystemState, set_system_state
    set_system_state(SystemState.NORMAL, "Previous release restored", state.get("request_id"))


def process_request(root: Path, state_dir: Path, request_path: Path) -> None:
    from core.system_state import SystemState, set_system_state
    from core.update_protocol import (download_package, fetch_manifest, validate_manifest,
                                      version_tuple)
    from core.version import application_version

    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_id, action = str(request.get("request_id", "")), request.get("action")
    if not re.fullmatch(r"[0-9a-f]{32}", request_id) or action not in {"check", "update"}:
        raise RuntimeError("Invalid update request")
    state = {"state": "RUNNING", "phase": "CHECKING", "action": action,
             "request_id": request_id, "message": "Checking signed release manifest",
             "updated_at": now(), "log": []}
    atomic_json(state_dir / "status.json", state)
    try:
        manifest = fetch_manifest(os.getenv("UPDATE_CHANNEL", "stable"))
        public_key = Path(os.getenv("UPDATE_PUBLIC_KEY", str(root / "installer" / "update-public.pem")))
        validate_manifest(manifest, public_key)
        current = application_version()
        state.update({"current_version": current, "available_version": manifest["version"],
                      "required": bool(manifest.get("required", False)),
                      "update_available": version_tuple(manifest["version"]) > version_tuple(current)})
        transition(state_dir, state, "CHECKING", "Signed release manifest verified")
        if action == "update" and state["update_available"]:
            wait_for_drain(state_dir, state)
            package = download_package(manifest, state_dir / "packages" / f"vertep-{manifest['version']}.tar.gz")
            transition(state_dir, state, "UPDATING", "Backup and package installation started")
            output = run(["/bin/bash", str(root / "scripts" / "vertep"), "apply-update",
                          str(package), manifest["version"]], root)
            state["log"].extend(output.splitlines()[-200:])
        state.update({"state": "SUCCEEDED", "phase": "NORMAL",
                      "message": "Update completed" if action == "update" else "Update check completed",
                      "updated_at": now()})
        set_system_state(SystemState.NORMAL, state["message"], request_id)
    except Exception as error:
        state.setdefault("log", []).append(f"{now()} {error}")
        if state.get("phase") == "UPDATING":
            try:
                transition(state_dir, state, "RECOVERING", "Health check failed; rolling back")
                state["log"].extend(run(["/bin/bash", str(root / "scripts" / "vertep"), "rollback"], root).splitlines()[-100:])
                state["state"] = "ROLLED_BACK"
                set_system_state(SystemState.NORMAL, "Automatic rollback completed", request_id)
            except Exception as rollback_error:
                state.update({"state": "FAILED", "phase": "EMERGENCY"})
                state["log"].append(f"{now()} rollback failed: {rollback_error}")
                set_system_state(SystemState.EMERGENCY, "Update and rollback failed", request_id)
        else:
            state.update({"state": "FAILED", "phase": "NORMAL"})
            set_system_state(SystemState.NORMAL, "Update stopped before installation", request_id)
        state.update({"message": str(error), "updated_at": now()})
    finally:
        state["log"] = state.get("log", [])[-500:]
        atomic_json(state_dir / "status.json", state)
        request_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process signed Vertep update requests")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    root, state_dir = args.root.resolve(), args.state_dir.resolve()
    sys.path.insert(0, str(root))
    if not (root / "scripts" / "vertep").is_file():
        raise SystemExit("Invalid Vertep installation root")
    recover_if_interrupted(root, state_dir)
    requests = state_dir / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    for request_path in sorted(requests.glob("*.json")):
        try:
            process_request(root, state_dir, request_path)
        except Exception as error:
            atomic_json(state_dir / "status.json", {"state": "FAILED", "phase": "NORMAL",
                        "action": None, "request_id": None, "message": str(error),
                        "updated_at": now(), "log": [f"{now()} {error}"]})
            request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
