"""Tests for Dispatcher shadow mode (Stage 5-7, Issue #300 i.0.0.3.0).

Tests shadow mode task routing, parity comparison between role and module
execution, and rollback to role path.
"""

from core.dispatcher import Dispatcher
from core.dispatcher import TaskRequest, TaskResponse


def test_dispatcher_assign_task_normal():
    dispatcher = Dispatcher()
    dispatcher.register_node("node-1", {"text_generation"}, {"vram_mb": 0}, "text")
    task = TaskRequest(
        task_id="task-1", task_type="text",
        capabilities_required={"text_generation"},
    )
    response = dispatcher.assign_task(task)
    assert response.accepted is True
    assert response.worker_id == "node-1"
    assert "text_generation" in response.capabilities_matched


def test_dispatcher_assign_task_no_candidate():
    dispatcher = Dispatcher()
    dispatcher.register_node("node-1", {"image_generation"}, {"vram_mb": 0}, "gpu")
    task = TaskRequest(
        task_id="task-1", task_type="text",
        capabilities_required={"text_generation"},
    )
    response = dispatcher.assign_task(task)
    assert response.accepted is False
    assert "No worker" in response.declined_reason


def test_dispatcher_shadow_mode_records_results():
    dispatcher = Dispatcher()
    dispatcher.register_node("node-text", {"text_generation"}, {"vram_mb": 0}, "text")
    task = TaskRequest(
        task_id="shadow-1", task_type="text",
        capabilities_required={"text_generation"},
        shadow_mode=True,
    )
    response = dispatcher.assign_task(task)
    assert response.accepted is True

    # Record role result
    dispatcher.record_shadow_result("shadow-1", "role", {
        "kind": "text", "data": "role output", "filename": "response.txt",
    })
    # Record module result
    dispatcher.record_shadow_result("shadow-1", "module", {
        "kind": "text", "data": "module output", "filename": "response.txt",
    })

    parity = dispatcher.get_shadow_parity("shadow-1")
    assert parity is not None
    assert parity["role_result"]["kind"] == "text"
    assert parity["module_result"]["kind"] == "text"
    assert parity["parity_ok"] is True


def test_dispatcher_shadow_parity_mismatch():
    dispatcher = Dispatcher()
    dispatcher.register_node("node-text", {"text_generation"}, {"vram_mb": 0}, "text")
    task = TaskRequest(
        task_id="shadow-2", task_type="text",
        capabilities_required={"text_generation"},
        shadow_mode=True,
    )
    dispatcher.assign_task(task)
    dispatcher.record_shadow_result("shadow-2", "role", {
        "kind": "text", "data": "role output",
    })
    dispatcher.record_shadow_result("shadow-2", "module", {
        "kind": "image", "data": "module output",
    })
    parity = dispatcher.get_shadow_parity("shadow-2")
    assert parity["parity_ok"] is False  # different kind


def test_dispatcher_shadow_rollback_to_role():
    dispatcher = Dispatcher()
    dispatcher.register_node("node-text", {"text_generation"}, {"vram_mb": 0}, "text")
    task = TaskRequest(
        task_id="shadow-3", task_type="text",
        capabilities_required={"text_generation"},
        shadow_mode=True,
    )
    dispatcher.assign_task(task)
    dispatcher.record_shadow_result("shadow-3", "role", {
        "kind": "text", "data": "role output",
    })
    dispatcher.record_shadow_result("shadow-3", "module", {
        "kind": "text", "data": "module output",
    })

    parity_before = dispatcher.get_shadow_parity("shadow-3")
    assert parity_before["parity_ok"] is True

    # Rollback
    assert dispatcher.rollback_to_role("shadow-3") is True
    parity_after = dispatcher.get_shadow_parity("shadow-3")
    assert parity_after["parity_ok"] is False
    assert parity_after.get("rolled_back") is True


def test_dispatcher_shadow_rollback_nonexistent():
    dispatcher = Dispatcher()
    assert dispatcher.rollback_to_role("no-such-task") is False


def test_dispatcher_shadow_no_results_yet():
    dispatcher = Dispatcher()
    dispatcher.register_node("node-text", {"text_generation"}, {"vram_mb": 0}, "text")
    task = TaskRequest(
        task_id="shadow-4", task_type="text",
        capabilities_required={"text_generation"},
        shadow_mode=True,
    )
    dispatcher.assign_task(task)
    # No results recorded yet
    parity = dispatcher.get_shadow_parity("shadow-4")
    assert parity is not None
    assert parity["role_result"] is None
    assert parity["module_result"] is None
    assert parity["parity_ok"] is None


def test_dispatcher_task_request_and_response_dataclasses():
    task = TaskRequest(
        task_id="t1", task_type="script",
        capabilities_required={"script_generation"},
        vram_mb=0,
        payload={"topic": "hello"},
        shadow_mode=True,
    )
    assert task.task_id == "t1"
    assert task.task_type == "script"
    assert task.shadow_mode is True
    assert task.payload["topic"] == "hello"

    response = TaskResponse(
        accepted=True, worker_id="node-1",
        capabilities_matched={"script_generation"},
    )
    assert response.accepted is True
    assert response.worker_id == "node-1"
