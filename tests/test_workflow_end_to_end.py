"""The shipped template runs topic → publication through the production worker entry point.

External boundaries are controlled (ComfyUI, TTS HTTP, the render itself); the engine,
the dispatcher, the polling transport and the executors are real.
"""

import base64
import json
from pathlib import Path
from time import monotonic

import pytest

from core.dispatcher import Dispatcher
from core.workflow.dsl import parse_workflow
from core.workflow.engine import WorkflowEngine
from core.workflow.runtime import PollingWorkerTransport
from core.workflow.state import WorkflowState
from tests.test_gpu_media_fixtures import MP4_DECODEABLE, PNG_VALID
from worker import role_executor, service


class Compute:
    def generate_output(self, workflow, prompt, kind):
        return PNG_VALID, "x.png", "image"

    def __getattr__(self, name):
        return lambda *args, **kwargs: (PNG_VALID, "x.png", "image")


class Engine:
    engine_id = "native"
    rendered = {}

    def render(self, output, **kwargs):
        Engine.rendered = kwargs
        Path(output).write_bytes(MP4_DECODEABLE)


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    for name, value in {"DEMO_MODE": "true", "NODE_ROLE": "core", "PUBLISHER_MOCK": "true",
                        "NODE_ADDITIONAL_ROLES": "text,voice,gpu,publisher",
                        "JOB_ROOT": str(tmp_path / "jobs")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PUBLISHER_DEFAULT_CHANNEL", raising=False)
    monkeypatch.setattr(role_executor, "providers", type("_P", (), {
        "compute": staticmethod(Compute), "video_engine": staticmethod(Engine)}))
    spoken = []

    def voice(task):
        spoken.append(task["topic"])
        return [role_executor._artifact("speech.wav", "audio", b"RIFF-audio")]

    monkeypatch.setitem(role_executor.EXECUTORS, "voice", voice)
    return spoken


def _run(tmp_path, params):
    definition = parse_workflow(Path("deploy/workflow-definitions/standard.yaml").read_text(encoding="utf-8"))
    for task in definition["tasks"]:
        task.pop("retry", None)  # a failure must surface now, not after the production backoff
    dispatcher = Dispatcher()
    transport = PollingWorkerTransport()
    dispatcher.register_node("all", {task["type"] for task in definition["tasks"]}, {"vram_mb": 48000}, "all")
    dispatcher.register_transport("all", transport)
    engine = WorkflowEngine(WorkflowState(state_root=tmp_path / "state"), dispatcher)
    job = engine.create_job(definition, params)
    engine.start_job(job)
    deadline = monotonic() + 30
    while monotonic() < deadline:
        engine.tick()
        status = engine.get_job_status(job)
        if status["status"] in {"COMPLETED", "FAILED"}:
            return status
        for task_id, task in status["tasks"].items():
            if task.get("status") == "COMPLETED" and task.get("approval") == "PENDING":
                engine.approve_task(job, task_id)
        issued = transport.poll()
        if issued:
            result = service.execute_task(Compute(), issued, "all")
            transport.result(issued["lease_id"], result.get("artifacts"),
                             result.get("error") if not result.get("success") else None)
    raise AssertionError(status)


def _body(artifacts, kind):
    item = next(entry for entry in artifacts if entry["kind"] == kind)
    return base64.b64decode(item["data_base64"])


def test_topic_reaches_publication_with_data_flowing_between_steps(tmp_path, worker_env):
    status = _run(tmp_path, {"topic": "Тема відео", "character_id": "c", "channel": "youtube"})
    assert status["status"] == "COMPLETED", status
    assert worker_env == ["Тема відео"]  # the demo script's voiceover reached the voice step
    assert Engine.rendered["audio"].name == "speech.wav"
    assert [path.suffix for path in Engine.rendered["images"]] == [".png"]
    receipt = json.loads(_body(status["tasks"]["publish"]["output"], "publication_receipt"))
    assert receipt["status"] == "PUBLISHED" and receipt["channel"] == "youtube"
    assert status["tasks"]["video"]["status"] == "SKIPPED"


def test_missing_channel_fails_the_publish_step(tmp_path, worker_env):
    status = _run(tmp_path, {"topic": "Тема", "character_id": "c"})
    assert status["status"] == "FAILED"
    assert "no channel" in status["tasks"]["publish"]["error"]


def test_unpublished_receipt_fails_the_step(tmp_path, worker_env, monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    status = _run(tmp_path, {"topic": "Тема", "character_id": "c", "channel": "unknown-channel"})
    assert status["status"] == "FAILED"
    assert "NOT_CONFIGURED" in status["tasks"]["publish"]["error"]
