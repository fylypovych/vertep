"""Tests for Workflow Engine checkpoint/resume (Stage 8-9, Issue #300 i.0.0.3.0).

Tests file-based WorkflowState persistence, checkpoint creation,
restore from checkpoint, and integration with backup snapshots.
"""

import json
import tempfile
from pathlib import Path

from core.workflow.dsl import validate_workflow
from core.workflow.engine import WorkflowEngine
from core.workflow.state import WorkflowState


def test_workflow_state_save_and_load(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    job_id = "job-001"
    job_state = {
        "id": job_id,
        "status": "IN_PROGRESS",
        "tasks": {
            "task-1": {"status": "COMPLETED", "progress_percent": 100},
            "task-2": {"status": "PENDING", "progress_percent": 0},
        },
        "progress": 50,
    }
    state.save_job(job_id, job_state)
    loaded = state.load_job(job_id)
    assert loaded is not None
    assert loaded["id"] == job_id
    assert loaded["status"] == "IN_PROGRESS"
    assert loaded["tasks"]["task-1"]["status"] == "COMPLETED"


def test_workflow_state_load_nonexistent():
    state = WorkflowState(storage_backend="file", state_root="/tmp/nonexistent-workflow-state")
    assert state.load_job("no-such-job") is None


def test_workflow_state_complete_and_fail_task(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    job_id = "job-002"
    state.save_job(job_id, {
        "id": job_id, "status": "IN_PROGRESS",
        "tasks": {"t1": {"status": "PENDING", "progress_percent": 0}},
        "progress": 0,
    })
    state.complete_task(job_id, "t1", result={"output": "done"})
    loaded = state.load_job(job_id)
    assert loaded["tasks"]["t1"]["status"] == "COMPLETED"
    assert loaded["tasks"]["t1"]["result"] == {"output": "done"}
    state.fail_task(job_id, "t1", error="boom")
    loaded = state.load_job(job_id)
    assert loaded["tasks"]["t1"]["status"] == "FAILED"
    assert loaded["tasks"]["t1"]["error"] == "boom"


def test_workflow_state_pause_resume(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    job_id = "job-003"
    state.save_job(job_id, {"id": job_id, "status": "IN_PROGRESS", "tasks": {}, "progress": 0})
    state.pause_job(job_id)
    assert state.load_job(job_id)["status"] == "PAUSED"
    state.resume_job(job_id)
    assert state.load_job(job_id)["status"] == "IN_PROGRESS"


def test_workflow_state_cancel(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    job_id = "job-004"
    state.save_job(job_id, {"id": job_id, "status": "IN_PROGRESS", "tasks": {}, "progress": 0})
    state.cancel_job(job_id)
    assert state.load_job(job_id)["status"] == "CANCELLED"


def test_workflow_state_checkpoint_and_restore(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    job_id = "job-005"
    state.save_job(job_id, {
        "id": job_id, "status": "IN_PROGRESS",
        "tasks": {"t1": {"status": "COMPLETED", "progress_percent": 100}},
        "progress": 50,
    })
    checkpoint = state.checkpoint(job_id)
    assert checkpoint is not None
    assert checkpoint["job_id"] == job_id
    assert checkpoint["state"]["id"] == job_id

    # Restore into a fresh state instance
    state2 = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state2"))
    assert state2.restore_from_checkpoint(checkpoint) is True
    restored = state2.load_job(job_id)
    assert restored is not None
    assert restored["status"] == "IN_PROGRESS"
    assert restored["tasks"]["t1"]["status"] == "COMPLETED"


def test_workflow_state_checkpoint_nonexistent():
    state = WorkflowState(storage_backend="file", state_root="/tmp/nonexistent-ws2")
    assert state.checkpoint("no-such-job") is None


def test_workflow_state_list_checkpoints(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    for jid in ("job-a", "job-b", "job-c"):
        state.save_job(jid, {"id": jid, "status": "NEW", "tasks": {}, "progress": 0})
    checkpoints = state.list_checkpoints()
    assert set(checkpoints) == {"job-a", "job-b", "job-c"}


def test_workflow_engine_create_and_status(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    engine = WorkflowEngine(state)
    definition = {
        "name": "test-workflow",
        "tasks": [
            {"id": "t1", "dependencies": []},
            {"id": "t2", "dependencies": ["t1"]},
        ],
    }
    errors = validate_workflow(definition)
    assert errors == []
    job_id = engine.create_job(definition)
    assert job_id is not None
    status = engine.get_job_status(job_id)
    assert status["status"] == "NEW"
    assert "t1" in status["tasks"]
    assert "t2" in status["tasks"]


def test_workflow_engine_start_and_complete(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    engine = WorkflowEngine(state)
    definition = {
        "name": "test-workflow",
        "tasks": [
            {"id": "t1", "dependencies": []},
        ],
    }
    job_id = engine.create_job(definition)
    result = engine.start_job(job_id)
    assert result["status"] == "COMPLETED"
    assert result["tasks"]["t1"]["status"] == "COMPLETED"


def test_workflow_engine_pause_resume(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    engine = WorkflowEngine(state)
    definition = {
        "name": "test-workflow",
        "tasks": [{"id": "t1", "dependencies": []}],
    }
    job_id = engine.create_job(definition)
    engine.start_job(job_id)
    paused = engine.pause_job(job_id)
    assert paused["status"] == "PAUSED"
    resumed = engine.resume_job(job_id)
    assert resumed["status"] == "COMPLETED"  # auto-executes on resume


def test_workflow_engine_cancel(tmp_path):
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "state"))
    engine = WorkflowEngine(state)
    definition = {
        "name": "test-workflow",
        "tasks": [{"id": "t1", "dependencies": []}],
    }
    job_id = engine.create_job(definition)
    cancelled = engine.cancel_job(job_id)
    assert cancelled["status"] == "CANCELLED"
