"""Dispatcher task assignment (Issue #300 i.0.0.3.0).

Shadow parity moved to worker/migration.py (tests/test_role_module_routing.py)."""

from core.dispatcher import Dispatcher
from core.dispatcher import TaskRequest


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
