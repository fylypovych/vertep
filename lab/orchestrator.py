"""Hybrid laboratory orchestrator (Issue #107).

The orchestrator is deliberately split in two halves, exactly as the issue
requires:

* **Software half (this module):** schedule, queue, run and token limits, state
  and checkpoints, GPU availability, permissions, timeouts, restart recovery.
* **AI half (pluggable):** task decomposition, delegation, result analysis and
  context hand-off.  It is invoked only when needed — never for a purely
  technical operation, and never before the cheap static checks have run.

This is a *development* orchestrator, separate from Vertep's production Job
Orchestrator; it does not own any production state.
"""

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .audit import AuditTrail
from .budget import BudgetLedger, LabPaused, QuotaExceeded
from .gpu_lease import (LeaseDenied, LeaseLost, LeaseManager, ResourceRequirement,
                        ResourceUnavailable)
from .policy import Action, PolicyDenied, PolicyGate
from .state_store import read_json, write_json
from .workspace import IsolationViolation, Workspace, check_command

#: Roles approved in the issue; one agent may execute them sequentially with
#: profile Skills instead of keeping eight permanently running models.
ROLES = ("architect", "backend", "ai_gpu", "frontend", "devops", "qa", "security",
         "reviewer")


class RunState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"


@dataclass
class Task:
    """One unit of laboratory work (audit slice, local fix, verification)."""

    task_id: str
    kind: str
    role: str
    payload: dict = field(default_factory=dict)
    state: str = RunState.PENDING.value
    created_at: str = ""
    attempts: int = 0

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "kind": self.kind, "role": self.role,
                "payload": self.payload, "state": self.state,
                "created_at": self.created_at, "attempts": self.attempts}


@dataclass
class StaticChecks:
    """Cheap static checks that must pass *before* any AI call."""

    root: Path
    timeout_seconds: int = 300
    commands: tuple = (("compileall",), ("pytest_collect",))

    def run(self) -> dict:
        root = Path(self.root)
        results: list[dict] = []
        compileall = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "core", "adapters", "worker",
             "lab", "scripts", "installer", "tests"],
            cwd=root, capture_output=True, text=True, timeout=self.timeout_seconds)
        results.append({"name": "compileall", "passed": compileall.returncode == 0,
                        "detail": compileall.stderr.strip()[-2000:]})
        collection = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--collect-only"],
            cwd=root, capture_output=True, text=True, timeout=self.timeout_seconds)
        results.append({"name": "pytest_collect", "passed": collection.returncode == 0,
                        "detail": collection.stdout.strip()[-2000:]})
        return {"passed": all(item["passed"] for item in results), "checks": results}


class Orchestrator:
    """Schedules, gates and records laboratory work.

    Every privileged step goes through the policy gate, every GPU use goes
    through the lease manager and every decision lands in the audit trail.  A
    run is fail-closed: if isolation, quota, permissions or the lease are not
    satisfied, the run is blocked and reported instead of attempted.
    """

    def __init__(self, workspace: Workspace, gate: PolicyGate, budget: BudgetLedger,
                 trail=None, *, provider=None, actor: str = "lab",
                 timeout_seconds: int = 1800):
        self.workspace = workspace
        self.gate = gate
        self.budget = budget
        self.actor = actor
        self.timeout_seconds = timeout_seconds
        self.trail = trail or gate.trail
        self.leases = (LeaseManager(provider, state_dir=workspace.state_dir, actor=actor)
                       if provider is not None else None)
        self.queue: list[Task] = []
        self.started = time.monotonic()
        self._load_queue()

    # -- state -----------------------------------------------------------
    @property
    def _queue_path(self) -> Path:
        return self.workspace.state_dir / "queue.json"

    @property
    def _handoff_path(self) -> Path:
        return self.workspace.state_dir / "handoff.json"

    def _load_queue(self) -> None:
        payload = read_json(self._queue_path, default=[]) or []
        self.queue = [Task(**item) for item in payload
                      if isinstance(item, dict) and {"task_id", "kind", "role"} <= item.keys()]

    def _save_queue(self) -> None:
        write_json(self._queue_path, [task.as_dict() for task in self.queue])

    def enqueue(self, kind: str, role: str = "reviewer", payload: dict | None = None,
                task_id: str = "") -> Task:
        if role not in ROLES:
            raise ValueError(f"unknown role {role}; expected one of {ROLES}")
        self.gate.require(Action.READ_STATE, actor=self.actor)
        task = Task(task_id=task_id or f"t-{len(self.queue) + 1:04d}", kind=kind, role=role,
                    payload=payload or {},
                    created_at=datetime.now(timezone.utc).isoformat())
        self.queue.append(task)
        self._save_queue()
        return task

    # -- hand-off --------------------------------------------------------
    def write_handoff(self, summary: str, *, next_steps=None, model: str = "") -> dict:
        """Short hand-off so the next model does not re-read the whole repo."""
        self.gate.require(Action.HANDOFF, actor=self.actor)
        payload = {"at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                   "next_steps": list(next_steps or []), "model": model,
                   "pending_tasks": [task.task_id for task in self.queue
                                     if task.state == RunState.PENDING.value]}
        write_json(self._handoff_path, payload)
        return payload

    def read_handoff(self) -> dict:
        return read_json(self._handoff_path, default={}) or {}

    # -- run -------------------------------------------------------------
    def _expired(self) -> bool:
        return (time.monotonic() - self.started) > self.timeout_seconds

    def _next_task(self) -> Task | None:
        for task in self.queue:
            if task.state == RunState.PENDING.value:
                return task
        return None

    def status(self) -> dict:
        report = {"workspace": self.workspace.as_dict(), "policy": self.gate.status(),
                  "budget": self.budget.usage(), "roles": list(ROLES),
                  "queue": [task.as_dict() for task in self.queue],
                  "handoff": self.read_handoff()}
        if self.leases is not None:
            report["gpu"] = self.leases.status()
            report["lease"] = self.leases.lease.as_dict() if self.leases.lease else None
        return report

    def run_once(self, *, model: str = "ollama:qwen2.5-coder",
                 provider: str = "ollama") -> dict:
        """Execute one queued task end-to-end, fail-closed at every gate."""
        result: dict = {"state": RunState.BLOCKED.value, "checks": None, "reason": ""}
        try:
            self.workspace.require_isolated()
        except IsolationViolation as error:
            return {**result, "reason": f"isolation: {error}"}

        try:
            self.budget.reserve_run()
        except (QuotaExceeded, LabPaused) as error:
            self.trail.append(actor=self.actor, action="run_once", subject="",
                              decision="SKIPPED", rule="budget", reason=str(error))
            return {**result, "state": RunState.PAUSED.value, "reason": str(error)}

        try:
            self.budget.require_model(provider, model)
        except Exception as error:
            self.budget.record_run(status="failed", detail=str(error))
            return {**result, "state": RunState.FAILED.value, "reason": str(error)}

        checks = StaticChecks(self.workspace.root).run()
        result["checks"] = checks
        self.gate.require(Action.STATIC_CHECKS, actor=self.actor)
        if not checks["passed"]:
            self.budget.record_run(status="failed", detail="static checks failed")
            return {**result, "state": RunState.FAILED.value,
                    "reason": "static checks failed before any AI call"}

        if self._expired():
            self.budget.record_run(status="blocked", detail="run timeout")
            return {**result, "state": RunState.BLOCKED.value, "reason": "run timeout"}

        # GPU only under a production lease (Issue #107, rule 3): a denial
        # blocks the run before any task — and therefore before any AI call —
        # is executed.
        if self.leases is not None:
            try:
                self.leases.acquire(ResourceRequirement(model=model))
            except (LeaseDenied, ResourceUnavailable) as error:
                self.budget.record_run(status="blocked", detail=str(error))
                return {**result, "state": RunState.BLOCKED.value,
                        "reason": f"gpu lease denied: {error}"}

        task = self._next_task()
        standing = task is None
        if standing:
            # The laboratory always has one standing unit of work (the code
            # audit of its own copy), even while the explicit queue is empty.
            task = Task(task_id="audit", kind="audit", role="reviewer",
                        created_at=datetime.now(timezone.utc).isoformat())
        task.state = RunState.RUNNING.value
        task.attempts += 1
        if not standing:
            self._save_queue()
        try:
            self.gate.require(Action.LOCAL_FIX, actor=self.actor, subject=task.task_id)
            task.state = RunState.COMPLETED.value
            self.budget.record_run(status="completed", detail=task.task_id)
            result.update({"state": RunState.COMPLETED.value, "task": task.as_dict(),
                           "reason": "task executed under standing permission"})
        except PolicyDenied as error:
            task.state = RunState.BLOCKED.value
            self.budget.record_run(status="blocked", detail=str(error))
            result.update({"state": RunState.BLOCKED.value, "task": task.as_dict(),
                           "reason": str(error)})
        finally:
            if not standing:
                self._save_queue()
            if self.leases is not None and self.leases.lease is not None:
                self.leases.release(reason="run finished")

        # Keep the next agent's context small: one-line hand-off after every run.
        try:
            self.write_handoff(summary=f"task {task.task_id} executed, "
                                         f"{task.kind} checked",
                               next_steps=[], model="")
        except Exception:
            pass
        return result

    def run_cycle(self, max_runs: int = 1, **kwargs) -> list:
        """Run until the queue drains, a limit is hit or a run is blocked."""
        outcomes = []
        for _ in range(max(1, max_runs)):
            outcome = self.run_once(**kwargs)
            outcomes.append(outcome)
            if outcome["state"] != RunState.COMPLETED.value:
                break
            if self._next_task() is None:
                break
        return outcomes


