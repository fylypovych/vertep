"""Configured workflow runtime and polling transport for existing Worker service."""

from concurrent.futures import Future
from pathlib import Path
from threading import RLock
import os

from core.dispatcher import Dispatcher
from .dsl import parse_workflow
from .engine import WorkflowEngine, synchronized
from .state import WorkflowState


class PollingWorkerTransport:
    def __init__(self):
        self._lock = RLock()
        self.active = None

    def is_available(self):
        with self._lock:
            return self.active is None

    def submit(self, request, lease):
        with self._lock:
            if self.active is not None:
                raise RuntimeError("Worker is busy")
            future = Future()
            self.active = {"request": request, "lease": lease, "future": future, "delivered": False, "cancelled": False}
            return future

    def cancel(self, lease):
        with self._lock:
            if self.active and self.active["lease"]["lease_id"] == lease["lease_id"]:
                self.active["cancelled"] = True

    def poll(self):
        with self._lock:
            active = self.active
            if not active or active["delivered"]:
                return None
            if active["cancelled"]:
                active["future"].set_exception(RuntimeError("Task cancelled before delivery"))
                self.active = None
                return None
            active["delivered"] = True
            request, lease = active["request"], active["lease"]
            task_type = {"text_generation": "script", "image_generation": "image", "video_generation": "video", "voice_synthesis": "voice", "video_assembly": "assembly", "publishing": "publish"}.get(request.task_type, request.task_type)
            return {**request.payload.get("params", {}), **request.payload, "task_id": request.task_id,
                    "job_id": request.payload["workflow_job_id"], "task": task_type,
                    "lease_id": lease["lease_id"], "protocol_version": "1.0.0", "architecture_workflow": True}

    def result(self, lease_id, result, error=None):
        with self._lock:
            active = self.active
            if not active or active["lease"]["lease_id"] != lease_id:
                return False
            future = active["future"]
            if error:
                future.set_exception(RuntimeError(error))
            else:
                future.set_result(result)
            self.active = None
            return True


class WorkflowRuntime:
    def __init__(self, root: Path):
        self._lock = RLock()
        self.root = root
        self.dispatcher = Dispatcher()
        self.transports = {}
        from core.module.registry import ModuleRegistry
        from core.module.discovery import ModuleDiscovery
        self.modules = ModuleRegistry({"core_runtime": (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()})
        self._module_reports = {}
        self.discovery = ModuleDiscovery(self.modules)
        backend = os.getenv("WORKFLOW_STATE_BACKEND", "file")
        if backend == "file":
            state = WorkflowState(state_root=os.getenv("WORKFLOW_STATE_ROOT", "workflow_state"))
        else:
            from .database_state import DatabaseWorkflowState
            state = DatabaseWorkflowState(backend, os.environ["WORKFLOW_STATE_URL"])
        self.engine = WorkflowEngine(state, self.dispatcher, module_registry=self.modules)

    def definitions(self):
        definitions = {}
        for path in sorted(self.root.glob("*.yaml")) + sorted(self.root.glob("*.json")):
            definitions[path.stem] = parse_workflow(path.read_text(encoding="utf-8"))
        return definitions

    def create(self, template, params, source_key=None):
        definition = self.definitions().get(template)
        if definition is None:
            raise ValueError("Unknown configured workflow template")
        with self.engine._lock:
            if source_key:
                for job_id, job in self.engine._jobs.items():
                    if job["params"].get("source_key") == source_key:
                        return job_id
                params = {**params, "source_key": source_key}
            job_id = self.engine.create_job(definition, params)
            from core.system_state import dispatch_allowed
            self.engine.start_job(job_id, allow_dispatch=dispatch_allowed())
            return job_id

    @synchronized
    def tick(self):
        from datetime import datetime, timezone
        from core.state import store
        from core.system_state import dispatch_allowed
        for name, info in list(self.dispatcher._node_registry.items()):
            worker = store.workers.get(name)
            if worker is None:
                self.dispatcher.unregister_node(name)
                continue
            try:
                info["last_heartbeat"] = datetime.fromisoformat(worker["last_seen"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError):
                info["last_heartbeat"] = datetime.min.replace(tzinfo=timezone.utc)
            active = self.transports[name].active
            info["resources"]["busy"] = bool(worker.get("current_task") and (not active or worker["current_task"] != active["lease"]["task_id"]))
        self.engine.tick(allow_dispatch=dispatch_allowed())

    def observe_legacy_job(self, legacy):
        """Represent the existing generation path as one task, without redispatch."""
        with self.engine._lock:
            workflow_id = legacy.workflow_execution_id
            if workflow_id is None or workflow_id not in self.engine._jobs:
                workflow_id = self.engine.create_job({"name": "Legacy generation", "version": "1.0.0", "tasks": [{"id": "generation", "type": "legacy_pipeline"}]}, {"legacy_job_id": legacy.job_id})
                legacy.workflow_execution_id = workflow_id
            job = self.engine._jobs[workflow_id]
            job["execution_mode"] = "legacy"
            state = str(getattr(legacy.status, "value", legacy.status))
            status = "IN_PROGRESS"
            if state in {"PUBLISHED", "READY"}:
                status = "COMPLETED"
            elif state == "CANCELLED":
                status = "CANCELLED"
            elif "FAILED" in state:
                status = "FAILED"
            elif state == "PAUSED":
                status = "PAUSED"
            elif state in {"NEW", "WAITING_FOR_SYSTEM"}:
                status = "NEW"
            job["status"] = status
            task = job["tasks"]["generation"]
            task.update(status={"IN_PROGRESS": "RUNNING", "NEW": "PENDING", "PAUSED": "RUNNING"}.get(status, status),
                        output={"job_id": legacy.job_id, "status": state, "artifacts": [item.model_dump(mode="json") for item in legacy.artifacts]},
                        approval=str(legacy.approval_status).upper() if "APPROVAL" in state else "APPROVED",
                        progress_percent=100 if status == "COMPLETED" else 0)
            job["progress"] = task["progress_percent"]
            self.engine.state.save_job(workflow_id, job)

    @synchronized
    def register(self, worker):
        from core.dispatcher import current_tested_capabilities, runtime_ready
        from core.protocol.task import compatible_protocol
        if not compatible_protocol(worker.get("protocol_version", "1.0.0")):
            raise ValueError("Incompatible worker protocol")
        if not runtime_ready(worker) or worker.get("desired_state") in {"DRAINING", "QUARANTINED", "DISABLED", "REVOKED", "RESTARTING", "UPDATING"}:
            raise ValueError("Worker is not ready")
        capabilities = current_tested_capabilities(worker)
        if not capabilities:
            transport = self.transports.get(worker["node_name"])
            if transport and transport.active and worker.get("current_task") == transport.active["lease"]["task_id"]:
                # A long task must not lose its ownership merely because its
                # pre-dispatch self-test aged while it occupied the worker.
                self.dispatcher.heartbeat(worker["node_name"])
                return transport
            raise ValueError("Worker has no current tested capabilities")
        name = worker["node_name"]
        previous = self.dispatcher.query_worker_status(name)
        previous_instance = (previous or {}).get("resources", {}).get("runtime_instance_id")
        current_instance = worker.get("runtime_instance_id")
        transport = self.transports.get(name)
        if previous_instance and current_instance and previous_instance != current_instance and transport and transport.active:
            lease = transport.active["lease"]
            self.dispatcher.revoke_task(lease["task_id"])
            transport.result(lease["lease_id"], None, "Worker process restarted")
        from core.module.manifest import ModuleManifest
        reported = {}
        for module in worker.get("module_inventory", []):
            manifest = ModuleManifest.from_dict(module)
            if module.get("ready") is True:
                reported[manifest.name] = manifest
        reports = {**self._module_reports, name: reported}
        from core.module.registry import ModuleRegistry
        rebuilt = ModuleRegistry(self.modules.system_versions)
        pending = {}
        for report in reports.values():
            pending.update(report)
        versions = {}
        while pending:
            loaded = [key for key, manifest in pending.items() if rebuilt.register_module(manifest, force=True)]
            if not loaded:
                raise ValueError("Worker module dependencies are incompatible")
            for key in loaded:
                manifest = pending.pop(key)
        for key, manifest in reported.items():
            versions[key] = manifest.version
            capabilities.update(manifest.capabilities)
        self.modules = rebuilt
        self._module_reports = reports
        self.engine.module_registry = rebuilt
        self.discovery.registry = rebuilt
        self.discovery.report_worker_capabilities(name, sorted(capabilities), worker)
        self.dispatcher.register_node(name, capabilities, {"vram_mb": worker.get("free_vram_mb") if worker.get("free_vram_mb") is not None else worker.get("vram_mb", 0),
            "cpu_cores": worker.get("cpu_cores") or 0, "ram_mb": worker.get("ram_mb") or 0,
            "runtime_instance_id": current_instance, "module_versions": versions}, worker["role"], worker.get("protocol_version", "1.0.0"))
        if name not in self.transports:
            self.transports[name] = PollingWorkerTransport()
            self.dispatcher.register_transport(name, self.transports[name])
        return self.transports[name]


_runtime = None
_runtime_lock = RLock()


def workflow_runtime():
    global _runtime
    root = os.getenv("WORKFLOW_DEFINITIONS_ROOT")
    if not root:
        return None
    with _runtime_lock:
        if _runtime is None or _runtime.root != Path(root):
            _runtime = WorkflowRuntime(Path(root))
        return _runtime
