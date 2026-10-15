#!/usr/bin/env python3
"""CORE-host rollout coordinator driver (Issue #144).

Runs as a systemd timer (single shot) or loop on the CORE host.  It is the CORE
equivalent of the worker update loop: it reads the durable rollout coordinator
record CORE writes (``core/rolling_update.py``), drives the *real* privileged host
update through the standard update-agent request mechanism (the same
``requests/<id>.json`` / ``status.json`` contract and cancel-fence the worker uses),
and reports the observed drain, applied version and bound self-test back through
the canonical ``update_core_coordinator`` write API.

It never writes a target version or a PASSED self-test on faith: every callback is
produced from the executor's own output (``status.json``), the on-disk application
version, and a freshly run self-test bound to the request CORE is waiting for.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")
_ACTIVE_COORDINATOR_STATES = {"DRAINING", "UPDATING", "SELF_TESTING", "ROLLBACK"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_root() -> Path:
    return Path(os.getenv("VERTEP_ROOT", str(Path(__file__).resolve().parents[1])))


def core_node_id() -> str:
    return os.getenv("VERTEP_CORE_NODE_ID", "core")


def update_state_dir(root: Path) -> Path:
    override = os.getenv("UPDATE_STATE_DIR")
    if override:
        return Path(override)
    from core.first_run import default_update_state_dir
    return default_update_state_dir()


def enqueue_host_request(state_dir: Path, action: str, target_version: str,
                         request_id: str) -> Path:
    """Write a privileged host-update request for the update-agent.

    Mirrors ``worker.service.request_local_update`` so the CORE host reuses the
    exact request contract (``requests/<request_id>.json`` + ``worker-update-target``
    marker) and honours the same cancel fence before mutating anything.
    """
    if action not in {"update", "rollback", "restart"}:
        raise ValueError("Unsupported host request action")
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must be 32 lowercase hexadecimal characters")
    requests = state_dir / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    fence_path = state_dir / "cancel-fence.json"
    if fence_path.exists():
        try:
            fence = json.loads(fence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Mutation blocked: cancel fence state is unreadable") from error
        raise RuntimeError(
            "Mutation blocked: rollout operation "
            f"{fence.get('operation_id')} was cancelled and this agent must not "
            "queue another host apply")
    marker = state_dir / "worker-update-target"
    marker_value = target_version if action == "update" else f"{action}:{target_version}"
    marker_value = f"{marker_value}:{request_id}"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == marker_value:
        return requests / f"{request_id}.json"
    temporary = requests / f".{request_id}.tmp"
    temporary.write_text(json.dumps({"request_id": request_id, "action": action,
                                     "target_version": target_version}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(requests / f"{request_id}.json")
    marker.write_text(marker_value, encoding="utf-8")
    return requests / f"{request_id}.json"


def read_host_status(state_dir: Path) -> dict:
    try:
        value = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def coordinator_record(rollout: dict, node_id: str) -> dict | None:
    return (rollout.get("core_coordinators") or {}).get(node_id)


@dataclass
class Host:
    """Injectable CORE-host capabilities so the driver stays unit-testable."""

    root: Path
    state_dir: Path
    current_version: Callable[[], str] = field(default=lambda: "0.0.0.0")
    host_status: Callable[[], dict] = field(default=lambda: {})
    enqueue: Callable[[str, str, str], object] = field(default=lambda action, version, request_id: None)
    drain_ready: Callable[[str], bool] = field(default=lambda operation_id: True)
    self_test: Callable[[dict | None, str], dict] = field(
        default=lambda request, version: {"status": "PASSED", "checked_at": now()})
    report: Callable[[str, dict], object] = field(default=lambda node_id, updates: None)


def default_host(root: Path, state_dir: Path) -> Host:
    from core.rolling_update import update_core_coordinator
    from core.system_state import SystemState, get_system_state, set_system_state
    from core.version import application_version

    def drain_ready(operation_id: str) -> bool:
        state = get_system_state()["state"]
        if state == SystemState.NORMAL.value:
            set_system_state(SystemState.UPDATING,
                             "Rolling update: draining CORE host", operation_id)
            state = SystemState.UPDATING.value
        if state not in {SystemState.UPDATING.value, SystemState.MAINTENANCE.value}:
            return False
        try:
            from core.repository import build_repository
            workers = list(build_repository(root).load_workers())
        except Exception:
            return False
        return all(not worker.get("current_task") for worker in workers)

    def self_test(request: dict | None, version: str) -> dict:
        from worker.service import bind_self_test, role_self_test
        return bind_self_test(role_self_test("core", {}), request, version)

    return Host(
        root=root,
        state_dir=state_dir,
        current_version=application_version,
        host_status=lambda: read_host_status(state_dir),
        enqueue=lambda action, version, request_id: enqueue_host_request(
            state_dir, action, version, request_id),
        drain_ready=drain_ready,
        self_test=self_test,
        report=update_core_coordinator,
    )


def drive_once(host: Host, node_id: str, rollout: dict) -> dict | None:
    """Advance the CORE target one step from real executor evidence.

    Returns the callback payload it reported, or ``None`` when there was nothing
    to do (or the mutation was fenced).  It never fabricates a target version or a
    PASSED self-test.
    """
    coordinator = coordinator_record(rollout, node_id)
    if not coordinator:
        return None
    state = coordinator.get("coordinator_state")
    if state not in _ACTIVE_COORDINATOR_STATES:
        return None
    operation_id = rollout.get("operation_id")
    if not isinstance(operation_id, str) or not _REQUEST_ID_RE.fullmatch(operation_id):
        return None

    try:
        if state == "DRAINING":
            if not host.drain_ready(operation_id):
                return None
            updates = {"drained": True, "version": host.current_version(),
                       "update_operation_id": operation_id}
        elif state == "UPDATING":
            target = coordinator.get("update_target_version")
            if not target:
                return None
            host.enqueue("update", target, operation_id)
            status = host.host_status()
            if status.get("state") != "SUCCEEDED" or host.current_version() != target:
                return None
            updates = {"version": target, "update_operation_id": operation_id}
        elif state == "SELF_TESTING":
            version = host.current_version()
            request = coordinator.get("self_test_request")
            if not isinstance(request, dict):
                return None
            updates = {"version": version,
                       "self_test": host.self_test(request, version)}
        elif state == "ROLLBACK":
            previous = coordinator.get("rollback_target_version")
            if not previous:
                return None
            host.enqueue("rollback", previous, operation_id)
            status = host.host_status()
            if status.get("state") != "ROLLED_BACK" or host.current_version() != previous:
                return None
            request = coordinator.get("self_test_request")
            updates = {"version": previous,
                       "self_test": host.self_test(request, previous)}
        else:  # pragma: no cover - guarded by _ACTIVE_COORDINATOR_STATES
            return None
    except RuntimeError:
        # Fenced mutation (cancel / lost lease): stop without writing.
        return None

    host.report(node_id, updates)
    return updates


def run_once(root: Path, state_dir: Path, node_id: str) -> dict | None:
    from core.rolling_update import rollout_status
    try:
        rollout = rollout_status()
    except Exception:
        return None
    if rollout.get("state") not in {"RUNNING", "ROLLING_BACK"}:
        return None
    if node_id not in {node.get("node_id") for node in rollout.get("nodes", [])}:
        return None
    return drive_once(default_host(root, state_dir), node_id, rollout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Drive the CORE-host rolling update")
    parser.add_argument("--root", type=Path, default=project_root())
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--node-id", default=core_node_id())
    parser.add_argument("--loop", action="store_true", help="Poll continuously instead of one shot")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    root = args.root.resolve()
    state_dir = args.state_dir.resolve() if args.state_dir else update_state_dir(root).resolve()
    while True:
        run_once(root, state_dir, args.node_id)
        if not args.loop:
            return
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
