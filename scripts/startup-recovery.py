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
    if status.get("state") != "UPDATING":
        return
    print(f"{now()} STARTUP-RECOVERY: interrupted update detected, rolling back", file=sys.stderr)
    rollback_dir = root / "runtime" / "updates"
    rollback_script = root / "scripts" / "release-layout.py"
    if rollback_script.is_file():
        output = run([sys.executable, str(rollback_script), "--rollback", str(root), str(state_dir)])
        print(f"{now()} STARTUP-RECOVERY: rollback result: {output}", file=sys.stderr)
    status["state"] = "FAILED"
    status["phase"] = "RECOVERED"
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
