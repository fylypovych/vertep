"""Workflow execution API; Worker messages retain per-node authentication."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.security import _valid_worker_request
from core.state import store
from core.workflow.runtime import workflow_runtime

router = APIRouter()


def runtime():
    instance = workflow_runtime()
    if instance is None:
        raise HTTPException(409, "Workflow templates are not configured")
    return instance


class WorkerMessage(BaseModel):
    node_name: str
    protocol_version: str = "1.0.0"
    task_id: str = ""
    lease_id: str = ""
    result: list | dict | str | None = None
    error: str | None = None
    progress_percent: float | None = Field(default=None, ge=0, le=100)


def worker_transport(payload, request, *, existing=False):
    from core.protocol.task import compatible_protocol
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    if not compatible_protocol(payload.protocol_version):
        raise HTTPException(409, "Incompatible workflow protocol")
    worker = store.workers.get(payload.node_name)
    if worker is None:
        raise HTTPException(409, "Worker registration required")
    instance = runtime()
    if existing and payload.node_name in instance.transports:
        return instance, instance.transports[payload.node_name]
    try:
        transport = instance.register(worker)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return instance, transport


@router.post("/api/tasks/claim/workflow")
def claim_workflow(payload: WorkerMessage, request: Request):
    from core.system_state import dispatch_allowed
    instance, transport = worker_transport(payload, request)
    worker = store.workers[payload.node_name]
    if not dispatch_allowed() or (worker.get("current_task") and not transport.active):
        return {"task": None}
    instance.tick()
    task = transport.poll()
    if task is not None:
        worker.update(status="BUSY", current_task=task["task_id"], current_job=task["job_id"])
        store.save_worker(worker)
    return {"task": task}


@router.post("/api/tasks/renew/workflow")
def renew_workflow(payload: WorkerMessage, request: Request):
    instance, transport = worker_transport(payload, request, existing=True)
    valid = instance.dispatcher.renew_lease(payload.task_id, payload.node_name, payload.lease_id)
    if valid and payload.progress_percent is not None:
        instance.engine.record_progress(payload.task_id, payload.node_name, payload.lease_id, payload.progress_percent)
    return {"renewed": valid, "cancelled": not valid or bool(transport.active and transport.active["cancelled"])}


@router.post("/api/tasks/result/workflow")
def result_workflow(payload: WorkerMessage, request: Request):
    instance, transport = worker_transport(payload, request, existing=True)
    with instance._lock, instance.engine._lock:
        active = transport.active
        if not active or active["lease"]["task_id"] != payload.task_id:
            raise HTTPException(409, "Stale workflow result")
        valid = instance.dispatcher.owns_lease(payload.task_id, payload.node_name, payload.lease_id)
        if not transport.result(payload.lease_id, payload.result, payload.error):
            raise HTTPException(409, "Stale workflow lease")
        worker = store.workers[payload.node_name]
        if worker.get("current_task") == payload.task_id:
            worker.update(status="READY", current_task=None, current_job=None)
            store.save_worker(worker)
        instance.tick()
        if not valid:
            raise HTTPException(409, "Revoked or expired workflow result rejected")
        if not payload.error and not any(task.get("dispatch_id") == payload.task_id and task["status"] == "COMPLETED"
                                         for job in instance.engine._jobs.values() for task in job["tasks"].values()):
            raise HTTPException(409, "Workflow result failed validation or task deadline")
        return {"accepted": True}


@router.get("/api/workflow-execution/templates")
def workflow_templates():
    return [{"id": name, "name": definition["name"]} for name, definition in runtime().definitions().items()]


@router.get("/api/workflow-execution/{job_id}")
def workflow_status(job_id: str):
    instance = runtime()
    instance.tick()
    return instance.engine.get_job_status(job_id)


class WorkflowAction(BaseModel):
    action: str
    task_id: str = ""
    expected_revision: int = Field(default=1, ge=1)
    input: dict = Field(default_factory=dict)


@router.post("/api/workflow-execution/{job_id}/action")
def workflow_action(job_id: str, payload: WorkflowAction):
    instance = runtime()
    engine = instance.engine
    if engine._jobs.get(job_id, {}).get("execution_mode") == "legacy":
        raise HTTPException(409, "Use the existing job actions for legacy generation")
    try:
        if payload.action in {"pause", "resume", "cancel"}:
            if payload.action == "resume":
                from core.system_state import dispatch_allowed
                return engine.resume_job(job_id, allow_dispatch=dispatch_allowed())
            return getattr(engine, payload.action + "_job")(job_id)
        if payload.action in {"approve", "reject"}:
            engine.approve_task(job_id, payload.task_id, payload.action == "approve", payload.expected_revision)
            instance.tick()
            return engine.get_job_status(job_id)
        if payload.action == "revise":
            engine.revise_task(job_id, payload.task_id, payload.input, payload.expected_revision)
            return engine.get_job_status(job_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(422, "Unknown workflow action")
