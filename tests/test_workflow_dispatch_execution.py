"""Actual worker transport execution, persisted outputs, and stale result fencing."""

from threading import Barrier, Event
from time import monotonic, sleep

import pytest

from core.dispatcher import Dispatcher, TaskRequest
from core.workflow.dsl import validate_workflow, parse_workflow
from core.workflow.engine import WorkflowEngine
from core.workflow.state import WorkflowState
from worker.protocol.transport import RoleWorkerTransport


def drive(engine, job, terminal="COMPLETED"):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        engine.tick()
        status = engine.get_job_status(job)
        if status["status"] == terminal:
            return status
        sleep(.005)
    raise AssertionError(engine.get_job_status(job))


@pytest.fixture
def execution(tmp_path):
    dispatcher = Dispatcher()
    transports = []

    def register(name, execute):
        transport = RoleWorkerTransport("text", execute=execute)
        dispatcher.register_node(name, {"text"}, {"vram_mb": 0}, "text")
        dispatcher.register_transport(name, transport)
        transports.append(transport)
        return transport

    engine = WorkflowEngine(WorkflowState(state_root=tmp_path), dispatcher)
    yield engine, register
    for transport in transports:
        transport.close()


def test_parallel_dependency_outputs_persist(execution):
    engine, register = execution
    barrier = Barrier(2)

    def execute(role, task):
        if task.get("root"):
            barrier.wait(timeout=3)
            return task["root"]
        return "".join(sorted(task["dependencies"].values()))

    register("a", execute)
    register("b", execute)
    job = engine.create_job({"name": "parallel", "tasks": [
        {"id": "join", "type": "text", "dependencies": ["a", "b"]},
        {"id": "a", "type": "text", "input": {"root": "A"}},
        {"id": "b", "type": "text", "input": {"root": "B"}},
    ]})
    engine.start_job(job)
    result = drive(engine, job)
    assert result["tasks"]["join"]["output"] == "AB"
    fresh = WorkflowEngine(WorkflowState(state_root=engine.state.state_root))
    assert fresh.get_job_status(job) == result


def test_retry_requires_actual_success(execution):
    engine, register = execution
    calls = []

    def execute(role, task):
        calls.append(task["task_id"])
        if len(calls) == 1:
            raise ValueError("temporary failure")
        return {"artifact": "actual-output"}

    register("a", execute)
    job = engine.create_job({"name": "retry", "tasks": [{"id": "a", "type": "text", "retry": {"max_attempts": 2}}]})
    engine.start_job(job)
    status = drive(engine, job)
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert status["tasks"]["a"]["output"] == {"artifact": "actual-output"}


def test_pause_approval_and_cancel_fence(execution):
    engine, register = execution
    release = Event()
    started = Event()

    def execute(role, task):
        started.set()
        assert release.wait(3)
        return "output"

    register("a", execute)
    job = engine.create_job({"name": "pause", "tasks": [
        {"id": "a", "type": "text", "approval_required": True},
        {"id": "b", "type": "text", "dependencies": ["a"]},
    ]})
    engine.start_job(job)
    assert started.wait(2)
    engine.pause_job(job)
    release.set()
    deadline = monotonic() + 3
    while engine.get_job_status(job)["tasks"]["a"]["status"] != "COMPLETED" and monotonic() < deadline:
        engine.tick()
        sleep(.005)
    assert engine.get_job_status(job)["tasks"]["b"]["status"] == "PENDING"
    engine.resume_job(job)
    assert engine.get_job_status(job)["tasks"]["b"]["status"] == "PENDING"
    engine.approve_task(job, "a")
    drive(engine, job)

    release.clear()
    job = engine.create_job({"name": "cancel", "tasks": [{"id": "a", "type": "text"}]})
    engine.start_job(job)
    engine.cancel_job(job)
    release.set()
    engine.tick()
    assert engine.get_job_status(job)["status"] == "CANCELLED"
    assert engine.get_job_status(job)["tasks"]["a"]["output"] is None


def test_timeout_does_not_release_running_worker(execution):
    engine, register = execution
    release = Event()
    transport = register("a", lambda role, task: release.wait(3))
    job = engine.create_job({"name": "timeout", "tasks": [{"id": "a", "type": "text", "timeout_minutes": .0001}]})
    engine.start_job(job)
    try:
        drive(engine, job, "FAILED")
        assert not transport.is_available()
        assert engine.get_job_status(job)["tasks"]["a"]["output"] is None
    finally:
        release.set()


def test_unconfigured_worker_never_simulates_success(execution):
    engine, _ = execution
    job = engine.create_job({"name": "no-worker", "tasks": [{"id": "a", "type": "text"}]})
    engine.start_job(job)
    assert engine.get_job_status(job)["tasks"]["a"]["status"] == "PENDING"
    assert "No worker" in engine.get_job_status(job)["tasks"]["a"]["error"]


def test_dsl_forward_refs_cycles_and_safe_yaml():
    assert not validate_workflow({"name": "x", "tasks": [{"id": "a", "dependencies": ["b"]}, {"id": "b"}]})
    assert any("Cyclic" in error for error in validate_workflow({"name": "x", "tasks": [{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}]}))
    assert parse_workflow('name: x\ntasks:\n  - id: a')["name"] == "x"


def test_duplicate_owner_and_stale_lease():
    dispatcher = Dispatcher()
    dispatcher.register_node("a", {"text"}, {}, "text")
    request = TaskRequest("task", "text")
    first = dispatcher.assign_task(request)
    assert first.accepted
    assert not dispatcher.assign_task(request).accepted
    assert not dispatcher.accept_result("task", "a", "wrong")
    dispatcher.revoke_task("task")
    second = dispatcher.assign_task(request)
    assert second.accepted and second.lease_id != first.lease_id
    assert not dispatcher.accept_result("task", "a", first.lease_id)
    assert dispatcher.accept_result("task", "a", second.lease_id)


def test_revision_invalidates_approval_and_descendants(execution):
    engine, register = execution
    register("a", lambda role, task: task.get("text", "derived"))
    job = engine.create_job({"name": "revision", "tasks": [
        {"id": "a", "type": "text", "input": {"text": "first"}, "approval_required": True},
        {"id": "b", "type": "text", "dependencies": ["a"]},
    ]})
    engine.start_job(job)
    deadline = monotonic() + 3
    while engine.get_job_status(job)["tasks"]["a"]["status"] != "COMPLETED" and monotonic() < deadline:
        engine.tick()
        sleep(.005)
    engine.approve_task(job, "a", expected_revision=1)
    engine.pause_job(job)
    engine.revise_task(job, "a", {"text": "second"}, 1)
    assert engine._jobs[job]["tasks"]["a"]["history"][0]["output"] == "first"
    engine.resume_job(job)
    deadline = monotonic() + 3
    while engine.get_job_status(job)["tasks"]["a"]["status"] != "COMPLETED" and monotonic() < deadline:
        engine.tick()
        sleep(.005)
    with pytest.raises(ValueError, match="Stale"):
        engine.approve_task(job, "a", expected_revision=1)
    assert engine.get_job_status(job)["tasks"]["b"]["status"] == "PENDING"
    engine.approve_task(job, "a", expected_revision=2)
    status = drive(engine, job)
    assert status["tasks"]["a"]["output"] == "second"


def test_worker_loss_falls_back_and_rejects_old_result(execution):
    from datetime import datetime, timezone, timedelta
    engine, register = execution
    release = Event()
    register("lost", lambda role, task: release.wait(3))
    register("fallback", lambda role, task: "fallback-output")
    job = engine.create_job({"name": "loss", "tasks": [{"id": "a", "type": "text", "retry": {"max_attempts": 2}}]})
    engine.start_job(job)
    engine.dispatcher._node_registry["lost"]["last_heartbeat"] = datetime.now(timezone.utc) - timedelta(seconds=46)
    try:
        status = drive(engine, job)
        assert status["tasks"]["a"]["output"] == "fallback-output"
        assert engine._jobs[job]["tasks"]["a"]["worker_id"] == "fallback"
        release.set()
        engine.tick()
        assert engine.get_job_status(job)["tasks"]["a"]["output"] == "fallback-output"
    finally:
        release.set()


def test_conditional_skip_unblocks_join(execution):
    engine, register = execution
    register("a", lambda role, task: "selected")
    job = engine.create_job({"name": "conditional", "tasks": [
        {"id": "a", "type": "text", "when": {"param": "extended", "equals": True}, "approval_required": True},
        {"id": "b", "type": "text", "dependencies": ["a"]},
    ]}, {"extended": False})
    engine.start_job(job)
    assert drive(engine, job)["tasks"]["a"]["status"] == "SKIPPED"


def test_interrupted_running_attempt_cannot_resume_without_reconciliation(execution):
    engine, register = execution
    release = Event()
    register("a", lambda role, task: release.wait(3))
    job = engine.create_job({"name": "interrupted", "tasks": [{"id": "a", "type": "text"}]})
    engine.start_job(job)
    try:
        fresh = WorkflowEngine(WorkflowState(state_root=engine.state.state_root))
        assert fresh.get_job_status(job)["status"] == "PAUSED"
        with pytest.raises(ValueError, match="reconciliation"):
            fresh.resume_job(job)
        assert fresh.get_job_status(job)["tasks"]["a"]["output"] is None
    finally:
        engine.cancel_job(job)
        release.set()
