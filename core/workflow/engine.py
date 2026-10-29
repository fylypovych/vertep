"""Workflow Engine - core component of Architecture 2.0.

Implements Workflow DAG execution with parallel task execution,
retries, timeouts, pause/resume/cancel support.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict
import json
from copy import deepcopy
from threading import RLock
from time import time
from functools import wraps

from core.dispatcher import Dispatcher, TaskRequest

from .dsl import validate_workflow
from .state import WorkflowState


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class WorkflowEngine:
    """Engine for executing workflow DAGs."""

    def __init__(self, state: WorkflowState | None = None, dispatcher: Dispatcher | None = None, module_registry=None) -> None:
        self.state = state or WorkflowState()
        self._jobs: Dict[str, dict] = {}
        self._running: Dict[str, set] = {}  # job_id -> set of running task IDs
        self.dispatcher = dispatcher or Dispatcher()
        self.module_registry = module_registry
        self._attempts = {}
        self._lock = RLock()
        self._load_existing_jobs()

    def _load_existing_jobs(self) -> None:
        """Restore in-memory job cache from persisted state."""
        try:
            job_ids = self.state.list_checkpoints()
        except Exception:
            job_ids = []
        for job_id in job_ids:
            job = self.state.load_job(job_id)
            if job is not None:
                for task in job["tasks"].values():
                    task.setdefault("approval", "APPROVED")
                    task.setdefault("attempts", 0)
                    if task["status"] == "RUNNING" and job.get("execution_mode") != "legacy":
                        # Restart does not prove a remote attempt has stopped.
                        # Preserve identity and require reconciliation before resume.
                        job["status"] = "PAUSED"
                        task["error"] = "Interrupted attempt requires lease reconciliation"
                self._jobs[job_id] = job
                self._running.setdefault(job_id, set())

    @synchronized
    def create_job(self, definition: dict, params: dict | None = None) -> str:
        """Create a new workflow job.

        Validates the definition and initializes job state.
        Returns the job_id.
        """
        errors = validate_workflow(definition)
        if errors:
            raise ValueError(f"Invalid workflow definition: {errors}")

        import uuid
        job_id = str(uuid.uuid4())

        self._jobs[job_id] = {
            "id": job_id,
            "definition": deepcopy(definition),
            "params": deepcopy(params or {}),
            "status": "NEW",
            "tasks": {},
            "progress": 0,
            "created_at": datetime.now(timezone.utc),
        }

        # Initialize task tracking
        for task in definition["tasks"]:
            self._jobs[job_id]["tasks"][task["id"]] = {
                "status": "PENDING",
                "progress_percent": 0,
                "output": None,
                "error": None,
                "dependencies": list(task.get("dependencies", [])),
                "type": task.get("type"),
                "attempts": 0,
                "revision": 1,
                "history": [],
                "approval": "PENDING" if task.get("approval_required") else "APPROVED",
            }

        self.state.save_job(job_id, self._jobs[job_id])
        return job_id

    @synchronized
    def start_job(self, job_id: str, allow_dispatch: bool = True) -> dict:
        """Start executing a workflow job."""
        if job_id not in self._jobs:
            raise ValueError(f"Job {job_id} not found")

        job = self._jobs[job_id]
        if job["status"] != "NEW":
            return self.get_job_status(job_id)
        job["status"] = "IN_PROGRESS"
        self.state.save_job(job_id, job)

        # Execute independent tasks in parallel
        self._execute_tasks(job_id, allow_dispatch)
        return self.get_job_status(job_id)

    def _execute_tasks(self, job_id: str, allow_dispatch: bool = True) -> None:
        """Advance persisted DAG through real worker Futures; safe to poll repeatedly."""
        with self._lock:
            job = self._jobs[job_id]
            if job.get("execution_mode") == "legacy":
                return
            definitions = {task["id"]: task for task in job["definition"]["tasks"]}
            for task_id, task in job["tasks"].items():
                definition = definitions[task_id]
                key = (job_id, task_id)
                active = self._attempts.get(key)
                if task["status"] == "RUNNING" and active:
                    lease, future = active
                    expired = time() >= task["deadline"] or not self.dispatcher.owns_lease(task["dispatch_id"], lease["worker_id"], lease["lease_id"])
                    if not future.done() and not expired:
                        continue
                    self._attempts.pop(key)
                    try:
                        if expired:
                            self.dispatcher.revoke_task(task["dispatch_id"])
                            raise TimeoutError("Task attempt timed out")
                        result = future.result()
                        json.dumps(result, allow_nan=False)
                        if not self.dispatcher.accept_result(task["dispatch_id"], lease["worker_id"], lease["lease_id"]):
                            raise RuntimeError("Stale worker result rejected")
                        task.update(status="COMPLETED", output=result, progress_percent=100, error=None)
                    except Exception as exc:
                        self.dispatcher.revoke_task(task["dispatch_id"])
                        retry = definition.get("retry", {})
                        task.update(error=str(exc), status="PENDING" if task["attempts"] < retry.get("max_attempts", 1) else "FAILED",
                                    retry_at=time() + retry.get("delay_seconds", 0))
                if not allow_dispatch or job["status"] != "IN_PROGRESS" or task["status"] != "PENDING":
                    continue
                deps = [job["tasks"][dep] for dep in task["dependencies"]]
                if not all(dep["status"] in {"COMPLETED", "SKIPPED"} and dep.get("approval", "APPROVED") == "APPROVED" for dep in deps):
                    continue
                condition = definition.get("when")
                if condition is not None and job["params"].get(condition["param"]) != condition["equals"]:
                    task.update(status="SKIPPED", progress_percent=100, approval="APPROVED")
                    continue
                if time() < task.get("retry_at", 0):
                    continue
                dispatch_id = f"{job_id}:{task_id}:r{task.get('revision', 1)}:{task.get('attempts', 0) + 1}"
                payload = deepcopy(definition.get("input", {}))
                capabilities = set(definition.get("capabilities_required", []))
                vram = definition.get("vram_mb", 0)
                if definition.get("module"):
                    manifest = self.module_registry.get_module(definition["module"]) if self.module_registry else None
                    if manifest is None:
                        task["error"] = "Required module is not discovered"
                        continue
                    payload["module_name"] = definition["module"]
                    payload["module_version"] = manifest.version
                    payload["resource_requirements"] = manifest.resource_requirements
                    capabilities.update(manifest.capabilities)
                    vram = max(vram, manifest.resource_requirements.get("min_vram_mb", 0))
                payload.update(workflow_job_id=job_id, params=deepcopy(job["params"]), dependencies={dep: deepcopy(job["tasks"][dep]["output"]) for dep in task["dependencies"]})
                payload["inference_operation_id"] = f"{job_id}:{task_id}:r{task.get('revision', 1)}"
                request = TaskRequest(task_id=dispatch_id, task_type=definition.get("type", ""),
                                      capabilities_required=capabilities,
                                      vram_mb=vram, payload=payload)
                try:
                    lease, future = self.dispatcher.submit_task(request)
                except RuntimeError as exc:
                    task["error"] = str(exc)
                    continue
                task.update(status="RUNNING", dispatch_id=dispatch_id, lease_id=lease["lease_id"],
                            worker_id=lease["worker_id"], attempts=task.get("attempts", 0) + 1,
                            deadline=time() + definition.get("timeout_minutes", 60) * 60)
                self._attempts[key] = (lease, future)
            tasks = list(job["tasks"].values())
            job["progress"] = int(100 * sum(task["status"] in {"COMPLETED", "SKIPPED"} for task in tasks) / len(tasks))
            if job["status"] == "IN_PROGRESS":
                if any(task["status"] == "FAILED" for task in tasks):
                    job["status"] = "FAILED"
                    for task_id, task in job["tasks"].items():
                        if task["status"] == "RUNNING":
                            self.dispatcher.revoke_task(task["dispatch_id"])
                            self._attempts.pop((job_id, task_id), None)
                            task["status"] = "CANCELLED"
                elif all(task["status"] in {"COMPLETED", "SKIPPED"} and task["approval"] == "APPROVED" for task in tasks):
                    job["status"] = "COMPLETED"
            self.state.save_job(job_id, job)

    def tick(self, allow_dispatch: bool = True) -> None:
        """Called by CORE scheduler to collect results and dispatch ready tasks."""
        for job_id in list(self._jobs):
            if self._jobs[job_id]["status"] in {"IN_PROGRESS", "PAUSED"}:
                self._execute_tasks(job_id, allow_dispatch)

    @synchronized
    def record_progress(self, dispatch_id: str, worker_id: str, lease_id: str, percent: float) -> bool:
        if not 0 <= percent <= 100 or not self.dispatcher.owns_lease(dispatch_id, worker_id, lease_id):
            return False
        for job_id, job in self._jobs.items():
            for task in job["tasks"].values():
                if task.get("dispatch_id") == dispatch_id and task["status"] == "RUNNING":
                    task["progress_percent"] = percent
                    self.state.save_job(job_id, job)
                    return True
        return False

    def approve_task(self, job_id: str, task_id: str, approved: bool = True, expected_revision: int | None = None) -> None:
        with self._lock:
            task = self._jobs[job_id]["tasks"][task_id]
            if task["status"] != "COMPLETED":
                raise ValueError("Only completed output can be approved")
            if expected_revision is not None and task.get("revision", 1) != expected_revision:
                raise ValueError("Stale workflow approval revision")
            task["approval"] = "APPROVED" if approved else "REJECTED"
            self.state.save_job(job_id, self._jobs[job_id])

    def revise_task(self, job_id: str, task_id: str, inputs: dict, expected_revision: int) -> None:
        """Invalidate a stage and its downstream outputs while preserving history."""
        with self._lock:
            job = self._jobs[job_id]
            if job["status"] != "PAUSED":
                raise ValueError("Pause workflow before revising a stage")
            if not isinstance(inputs, dict) or job["tasks"][task_id].get("revision", 1) != expected_revision:
                raise ValueError("Invalid input or stale workflow revision")
            affected = {task_id}
            while True:
                expanded = affected | {key for key, task in job["tasks"].items() if affected.intersection(task["dependencies"])}
                if expanded == affected:
                    break
                affected = expanded
            definitions = {task["id"]: task for task in job["definition"]["tasks"]}
            definitions[task_id]["input"] = deepcopy(inputs)
            for key in affected:
                task = job["tasks"][key]
                if task["status"] == "RUNNING":
                    self.dispatcher.revoke_task(task["dispatch_id"])
                    self._attempts.pop((job_id, key), None)
                task.setdefault("history", []).append({"revision": task.get("revision", 1), "output": task.get("output"), "approval": task["approval"]})
                task.update(status="PENDING", output=None, progress_percent=0, error=None, attempts=0, retry_at=0,
                            revision=task.get("revision", 1) + 1,
                            approval="PENDING" if definitions[key].get("approval_required") else "APPROVED")
            self.state.save_job(job_id, job)

    @synchronized
    def pause_job(self, job_id: str) -> dict:
        """Pause a running job."""
        if job_id not in self._jobs:
            raise ValueError(f"Job {job_id} not found")

        job = self._jobs[job_id]
        if job["status"] not in {"IN_PROGRESS", "FAILED", "COMPLETED"}:
            return self.get_job_status(job_id)

        job["status"] = "PAUSED"
        self.state.save_job(job_id, job)
        return self.get_job_status(job_id)

    @synchronized
    def resume_job(self, job_id: str, allow_dispatch: bool = True) -> dict:
        """Resume a paused job."""
        if job_id not in self._jobs:
            raise ValueError(f"Job {job_id} not found")

        job = self._jobs[job_id]
        if job["status"] != "PAUSED":
            return self.get_job_status(job_id)
        if any(task["status"] == "RUNNING" and (job_id, key) not in self._attempts
               for key, task in job["tasks"].items()):
            raise ValueError("Interrupted attempt requires lease reconciliation before resume")

        job["status"] = "IN_PROGRESS"
        self.state.save_job(job_id, job)
        self._execute_tasks(job_id, allow_dispatch)
        return self.get_job_status(job_id)

    @synchronized
    def cancel_job(self, job_id: str) -> dict:
        """Cancel a job."""
        if job_id not in self._jobs:
            raise ValueError(f"Job {job_id} not found")

        job = self._jobs[job_id]
        job["status"] = "CANCELLED"
        for task_id, task in job["tasks"].items():
            if task["status"] == "RUNNING":
                self.dispatcher.revoke_task(task["dispatch_id"])
                self._attempts.pop((job_id, task_id), None)
                task["status"] = "CANCELLED"
        self.state.save_job(job_id, job)
        return self.get_job_status(job_id)

    @synchronized
    def get_job_status(self, job_id: str) -> dict:
        """Get current status of a job."""
        if job_id not in self._jobs:
            return {"status": "NOT_FOUND", "tasks": {}, "progress": 0}

        job = self._jobs[job_id]
        return {
            "status": job["status"],
            "tasks": {k: {
                "status": v["status"],
                "progress_percent": v["progress_percent"],
                "error": v.get("error"),
                "output": deepcopy(v.get("output")),
                "approval": v.get("approval", "APPROVED"),
                "revision": v.get("revision", 1),
            } for k, v in job["tasks"].items()},
            "progress": job["progress"],
        }
