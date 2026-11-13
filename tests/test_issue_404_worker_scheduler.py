"""i.0.0.4.4: Worker and Scheduler stay live when CORE disowns a task or a service fails."""
from concurrent.futures import Future
from types import SimpleNamespace

import httpx
import pytest

from core.api import job_helpers
from core.models import JobStatus
from core.pipeline import JobStore
from core.repository import MemoryRepository
from worker import role_executor, service

CORE = "http://core"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [404, 409])
def test_disowned_result_is_dropped(status):
    with _client(lambda request: httpx.Response(status, json={})) as client:
        service.submit_result(client, CORE, {"task_id": "t", "job_id": "j", "success": True})


def test_other_result_errors_still_surface():
    with _client(lambda request: httpx.Response(500, json={})) as client:
        with pytest.raises(httpx.HTTPStatusError):
            service.submit_result(client, CORE, {"task_id": "t", "job_id": "j", "success": True})


@pytest.mark.parametrize("status", [404, 409])
def test_lost_lease_cancels_once_and_marks_the_task(status):
    task = {"task_id": "t"}
    with _client(lambda request: httpx.Response(status, json={})) as client:
        assert service.renew_active_task(client, CORE, "n", task) == [{"task_id": "t"}]
    assert task["lease_lost"] is True


def test_transient_renew_failure_keeps_the_lease_and_the_loop():
    task = {"task_id": "t"}
    with _client(lambda request: httpx.Response(503, json={})) as client:
        assert service.renew_active_task(client, CORE, "n", task) == []
    assert "lease_lost" not in task


def test_scheduler_failure_falls_back_to_the_local_rule(monkeypatch):
    monkeypatch.setenv("SCHEDULER_URL", "http://scheduler")

    def fail(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(job_helpers.common.httpx, "post", fail)
    job = SimpleNamespace(scheduled_for=None, model_dump=lambda mode: {})
    assert job_helpers._job_is_due(job) is True
    job.scheduled_for = "2999-01-01T00:00:00Z"
    assert job_helpers._job_is_due(job) is False


def test_one_preparation_per_job(monkeypatch):
    from core import runtime
    pending = Future()
    calls = []

    def submit(function, job):
        calls.append(job.job_id)
        return pending

    job = SimpleNamespace(job_id="job-1")
    with monkeypatch.context() as patch:  # shared pool: fixtures use it after the test
        patch.setattr(runtime.executor, "submit", submit)
        runtime._submit_prepare(job)
    assert "job-1" in runtime._preparing
    pending.set_result(None)
    assert "job-1" not in runtime._preparing
    assert calls == ["job-1"]


def test_persisted_scripting_job_recovers_after_restart(tmp_path):
    repository = MemoryRepository()
    store = JobStore(str(tmp_path), repository=repository)
    job = store.create("тема", "character", 1)
    store.update(job, JobStatus.SCRIPTING, "SCHEDULED JOB STARTED")
    reloaded = JobStore(str(tmp_path), repository=repository)
    assert reloaded.jobs[job.job_id].status == JobStatus.NEW


def test_stale_reference_does_not_recreate_a_deleted_job(tmp_path):
    repository = MemoryRepository()
    store = JobStore(str(tmp_path), repository=repository)
    job = store.create("тема", "character", 1)
    store.delete(job.job_id)
    store.update(job, JobStatus.FAILED, "STALE")
    store.event(job, "STALE")
    assert job.job_id not in store.jobs
    assert job.job_id not in {item.job_id for item in repository.load_jobs()}


def test_video_without_workflow_fails_clearly(monkeypatch):
    monkeypatch.delenv("COMFYUI_DEFAULT_VIDEO_WORKFLOW", raising=False)
    with pytest.raises(RuntimeError, match="Відео-workflow не налаштовано"):
        role_executor.execute_video({"topic": "тема"})


def test_video_workflow_returning_a_frame_is_assembled_into_video(monkeypatch, tmp_path):
    """AGENTS.md §14.3: a frame from ComfyUI becomes a video via FFmpeg on the node."""
    from tests.test_assembly_worker_route import _mp4
    from tests.test_mpt_media_matrix import _png
    frame = _png(tmp_path / "f.png", (10, 20, 30), 16, 16).read_bytes()
    seen = {}

    class Compute:
        def generate_output(self, workflow, prompt, kind):
            return frame, "out.png", "image"

    class Assembly:
        def assemble(self, output, image=None, aspect_ratio="16:9", **_):
            seen["frame"], seen["aspect"] = image.read_bytes(), aspect_ratio
            return _mp4(output)

    monkeypatch.setattr(role_executor.providers, "compute", lambda: Compute())
    monkeypatch.setattr(role_executor.providers, "assembly", lambda: Assembly())
    [artifact] = role_executor.execute_video({"topic": "тема", "workflow": "wf.json", "aspect_ratio": "9:16"})
    assert seen == {"frame": frame, "aspect": "9:16"}
    assert artifact["filename"] == "out.mp4" and artifact["contract"]["mime_type"] == "video/mp4"
