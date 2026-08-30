#!/usr/bin/env python3
"""Worker-side update processor: handles update requests from Core."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def run(cmd: list[str], **kwargs) -> str:
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        return f"ERROR: {error}"


def get_root() -> Path:
    return Path(os.getenv("VERTEP_ROOT", Path(__file__).resolve().parent.parent))


def process_worker_update(root: Path, state_dir: Path, request_path: Path) -> None:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    action = request.get("action")
    if action == "rollback":
        status = {"state": "UPDATING", "phase": "ROLLING_BACK", "action": action,
                  "request_id": request.get("request_id"), "updated_at": now(),
                  "log": [f"{now()} Worker rollback started"]}
        atomic_json(state_dir / "status.json", status)
        output = run(["/bin/bash", str(root / "scripts" / "vertep"), "rollback"], timeout=600)
        if output.startswith("ERROR"):
            raise RuntimeError(f"Rollback failed: {output}")
        status.update({"state": "NORMAL", "phase": "ROLLED_BACK", "updated_at": now()})
        status["log"].append(f"{now()} Worker rollback completed")
        atomic_json(state_dir / "status.json", status)
        return
    if action != "update":
        raise ValueError("Unsupported worker update action")
    target_version = request.get("target_version")
    if not target_version:
        raise ValueError("Missing target_version in update request")
    status = {"state": "UPDATING", "phase": "DOWNLOADING", "action": "update",
              "request_id": request.get("request_id"), "target_version": target_version,
              "updated_at": now(), "log": [f"{now()} Worker update started"]}
    atomic_json(state_dir / "status.json", status)
    update_dir = root / "runtime" / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    package = update_dir / f"vertep-{target_version}.tar.gz"
    if not package.is_file():
        download_status = {"state": "UPDATING", "phase": "DOWNLOADING",
                           "updated_at": now(), "log": status["log"] + [f"{now()} Downloading {target_version}"]}
        atomic_json(state_dir / "status.json", download_status)
        output = run(["curl", "-fsSL", f"{os.getenv('VERTEP_UPDATE_SERVER', 'https://update.vertep.ai')}/v1/updates/{target_version}/package.tar.gz",
                     "-o", str(package)], timeout=600)
        if output.startswith("ERROR"):
            raise RuntimeError(f"Download failed: {output}")
    verify_status = {"state": "UPDATING", "phase": "VERIFYING",
                     "updated_at": now(), "log": status["log"] + [f"{now()} Verifying package"]}
    atomic_json(state_dir / "status.json", verify_status)
    verify_output = run(["python3", str(root / "scripts" / "safe-extract.py"), "--verify-only", str(package)])
    if verify_output.startswith("ERROR"):
        raise RuntimeError(f"Verification failed: {verify_output}")
    apply_status = {"state": "UPDATING", "phase": "APPLYING",
                    "updated_at": now(), "log": status["log"] + [f"{now()} Applying update"]}
    atomic_json(state_dir / "status.json", apply_status)
    apply_output = run([sys.executable, str(root / "scripts" / "apply-deployment.py"), "--root", str(root)])
    if apply_output.startswith("ERROR"):
        raise RuntimeError(f"Apply failed: {apply_output}")
    self_test_status = {"state": "UPDATING", "phase": "SELF_TESTING",
                        "updated_at": now(), "log": status["log"] + [f"{now()} Running self-test"]}
    atomic_json(state_dir / "status.json", self_test_status)
    time.sleep(5)
    final_status = {"state": "NORMAL", "phase": "COMPLETED", "action": "update",
                    "request_id": request.get("request_id"), "target_version": target_version,
                    "updated_at": now(), "log": status["log"] + [f"{now()} Update completed"]}
    atomic_json(state_dir / "status.json", final_status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker-side update processor")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    root, state_dir, request_path = args.root.resolve(), args.state_dir.resolve(), args.request.resolve()
    sys.path.insert(0, str(root))
    try:
        process_worker_update(root, state_dir, request_path)
    except Exception as error:
        atomic_json(state_dir / "status.json", {"state": "FAILED", "phase": "ERROR",
                    "action": "update", "request_id": None, "message": str(error),
                    "updated_at": now(), "log": [f"{now()} {error}"]})
        raise
    finally:
        request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
