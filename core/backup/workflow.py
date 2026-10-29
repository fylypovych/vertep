"""Capture/restore persisted workflow and declarative module state without deletion."""

import base64
from copy import deepcopy
import hashlib
import os
import re
from pathlib import Path

import yaml


def workflow_state():
    backend = os.getenv("WORKFLOW_STATE_BACKEND", "file")
    if backend == "file":
        from core.workflow.state import WorkflowState
        return WorkflowState(state_root=os.getenv("WORKFLOW_STATE_ROOT", "workflow_state"))
    from core.workflow.database_state import DatabaseWorkflowState
    return DatabaseWorkflowState(backend, os.environ["WORKFLOW_STATE_URL"])


def safe_path(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root or Path(name).is_absolute():
        raise ValueError("Snapshot state path escapes its configured root")
    return path


def capture(snapshot):
    state = workflow_state()
    snapshot.workflow_checkpoints = {job: {"job_id": job, "state": deepcopy(state.load_job(job))} for job in state.list_checkpoints()}
    modules_root = Path(os.getenv("MODULE_MANIFEST_ROOT", str(Path(__file__).resolve().parents[2] / "modules")))
    data_root = Path(os.getenv("MODULE_STATE_ROOT", "module_state"))
    for manifest in modules_root.glob("*/manifest.yaml"):
        definition = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        definition = definition.get("manifest", definition)
        files = {}
        for name in definition.get("backup", {}).get("paths", []):
            path = safe_path(data_root, name)
            if path.is_file():
                data = path.read_bytes()
                files[name] = {"data_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
        snapshot.module_state[definition["name"]] = {"version": definition["version"], "files": files}
    snapshot.checksum = snapshot.compute_checksum()
    return snapshot


def restore(snapshot):
    if not snapshot.verify():
        raise ValueError("Snapshot integrity check failed")
    data_root = Path(os.getenv("MODULE_STATE_ROOT", "module_state"))
    files = []
    for module in snapshot.module_state.values():
        for name, record in module.get("files", {}).items():
            path = safe_path(data_root, name)
            data = base64.b64decode(record["data_base64"], validate=True)
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("Module state checksum mismatch")
            files.append((path, data))
    checkpoints = deepcopy(snapshot.workflow_checkpoints)
    for key, checkpoint in checkpoints.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("Invalid workflow checkpoint identity")
        if checkpoint.get("job_id") != key or not isinstance(checkpoint.get("state"), dict):
            raise ValueError("Invalid workflow checkpoint")
        job = checkpoint["state"]
        if job["status"] not in {"COMPLETED", "CANCELLED", "FAILED"}:
            job["status"] = "PAUSED"
        for task in job["tasks"].values():
            if task["status"] == "RUNNING":
                task.update(status="PENDING", error="Restored attempt fenced; new lease required")
                task.pop("lease_id", None)
    for path, data in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".restore.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    state = workflow_state()
    for checkpoint in checkpoints.values():
        state.restore_from_checkpoint(checkpoint)
    return len(checkpoints)
