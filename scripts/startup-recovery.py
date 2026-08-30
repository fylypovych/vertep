#!/usr/bin/env python3
"""Startup recovery: restore system state after power loss/crash."""

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


def get_root() -> Path:
    return Path(os.getenv("VERTEP_ROOT", Path(__file__).resolve().parent.parent))


def get_update_state_dir() -> Path:
    return Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update"))


def recover_interrupted_update(root: Path, state_dir: Path) -> None:
    status_file = state_dir / "status.json"
    if not status_file.exists():
        return
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    phase = status.get("phase")
    if status.get("state") != "RUNNING" or phase not in {"UPDATING", "RECOVERING"}:
        return
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


def restore_system_state(root: Path, state_dir: Path) -> None:
    system_state_file = state_dir / "system-state.json"
    if not system_state_file.exists():
        return
    try:
        state = json.loads(system_state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    current_state = state.get("state", "NORMAL")
    if current_state in {"MAINTENANCE", "UPDATING", "RECOVERING"}:
        print(f"{now()} STARTUP-RECOVERY: restoring system state from {current_state} to NORMAL", file=sys.stderr)
        state["state"] = "NORMAL"
        state["updated_at"] = now()
        state["reason"] = "Startup recovery"
        system_state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def restart_containers(root: Path) -> str:
    compose = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "docker-compose.yml")]
    try:
        environment = (root / ".env").read_text(encoding="utf-8")
    except OSError:
        environment = ""
    if "GPU_VENDOR=amd" in environment and (root / "docker-compose.amd.yml").is_file():
        compose.extend(["-f", str(root / "docker-compose.amd.yml")])
    if "GPU_VENDOR=nvidia" in environment and (root / "docker-compose.nvidia.yml").is_file():
        compose.extend(["-f", str(root / "docker-compose.nvidia.yml")])
    try:
        subprocess.run([*compose, "up", "-d", "--remove-orphans"], check=True, capture_output=True, text=True, timeout=300)
        return "containers restarted"
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        return f"ERROR: {error}"


def main() -> None:
    root = get_root()
    state_dir = get_update_state_dir()
    print(f"{now()} STARTUP-RECOVERY: checking system state", file=sys.stderr)
    recover_interrupted_update(root, state_dir)
    restore_system_state(root, state_dir)
    output = restart_containers(root)
    print(f"{now()} STARTUP-RECOVERY: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
