"""The shipped full-process template is valid, ordered, and not enabled by itself."""

from pathlib import Path
from time import monotonic, sleep

import pytest

from core.dispatcher import Dispatcher
from core.workflow.dsl import parse_workflow
from core.workflow.engine import WorkflowEngine
from core.workflow.state import WorkflowState
from worker.protocol.transport import RoleWorkerTransport

TEMPLATE = Path("deploy/workflow-definitions/standard.yaml")


def _definition():
    return parse_workflow(TEMPLATE.read_text(encoding="utf-8"))


def _run(tmp_path, params):
    definition = _definition()
    types = {task["type"] for task in definition["tasks"]}
    for task in definition["tasks"]:
        task.pop("approval_required", None)
    order = []

    def execute(role, task):
        order.append(task["task"] if "task" in task else task["stage"])
        return {"done": True}

    for task in definition["tasks"]:
        task["input"] = {"stage": task["id"]}
    dispatcher = Dispatcher()
    transport = RoleWorkerTransport("all", execute=execute)
    dispatcher.register_node("all", types, {"vram_mb": 48000}, "all")
    dispatcher.register_transport("all", transport)
    engine = WorkflowEngine(WorkflowState(state_root=tmp_path), dispatcher)
    try:
        job = engine.create_job(definition, params)
        engine.start_job(job)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            engine.tick()
            status = engine.get_job_status(job)
            if status["status"] == "COMPLETED":
                return order, status
            sleep(.005)
        raise AssertionError(status)
    finally:
        transport.close()


def test_template_covers_the_process_and_requires_both_approvals():
    tasks = {task["id"]: task for task in _definition()["tasks"]}
    assert list(tasks) == ["script", "voice", "images", "video", "assembly", "publish"]
    assert tasks["script"]["approval_required"] and tasks["assembly"]["approval_required"]


@pytest.mark.parametrize("params,expected_video", [({"generate_video": True}, True), ({}, False)])
def test_order_and_optional_video_step(tmp_path, params, expected_video):
    order, status = _run(tmp_path, params)
    assert order[0] == "script" and order[-2:] == ["assembly", "publish"]
    assert order.index("images") < order.index("assembly") and order.index("voice") < order.index("assembly")
    assert ("video" in order) is expected_video
    assert status["status"] == "COMPLETED"


def test_template_is_not_enabled_by_the_install():
    assert "WORKFLOW_DEFINITIONS_ROOT" not in Path("bootstrap.sh").read_text(encoding="utf-8")
    assert "WORKFLOW_DEFINITIONS_ROOT" not in Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
