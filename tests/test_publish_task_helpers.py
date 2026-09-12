"""Unit tests for publish-task helpers in core.api.job_helpers.

Tests:
  - _handle_publish_result success path stores receipt and marks channel.
  - _handle_publish_result NOT_CONFIGURED path does not retry.
  - _handle_publish_result failure path retries up to max_retries.
  - _handle_publish_result dead-letters after max_retries exhausted.
  - _publish_local stores receipt and updates publication_results.
  - _enqueue_publish_task local fallback skips queue.
  - _recover_stale_workers requeues publish tasks.
"""
import json
import base64
from unittest.mock import MagicMock, patch

import httpx

from publishers import FakeTransport
from core.models import Job, JobStatus
from core.api.job_helpers import (
    _handle_publish_result,
    _publish_local,
    _enqueue_publish_task,
)


def _make_job(**kwargs):
    defaults = dict(
        job_id="job-test",
        topic="Test",
        character_id="char",
        priority=5,
        status=JobStatus.PUBLISHING,
        created_at="2024-01-01T00:00:00Z",
        publish_task_ids={},
        publish_attempt=0,
        publication_results={},
        published_to=[],
        max_retries=3,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _make_store():
    store = MagicMock()
    store.workers = {}
    store.repository = MagicMock()
    store.jobs = {}

    def fake_update(job, status, msg=""):
        job.status = status
        job_events = store.jobs.setdefault(job.job_id, [])
        job_events.append(msg)

    store.update.side_effect = fake_update
    return store


def _receipt_artifact(channel, status, error=None):
    receipt = {"channel": channel, "status": status}
    if error:
        receipt["error"] = error
    data = json.dumps(receipt).encode("utf-8")
    return {"kind": "publication_receipt", "data_base64": base64.b64encode(data).decode("utf-8")}


# ---------------------------------------------------------------------------


def test_publish_result_success_stores_receipt():
    job = _make_job()
    job.publish_task_ids = {"task-1": "youtube"}
    store = _make_store()
    artifacts = [_receipt_artifact("youtube", "PUBLISHED")]
    _handle_publish_result(store, job, {"success": True, "task_id": "task-1"}, artifacts, "youtube")
    assert job.publish_task_ids == {}
    assert job.publication_results["youtube"]["status"] == "PUBLISHED"
    assert "youtube" in job.published_to


def test_publish_result_not_configured_does_not_retry():
    job = _make_job()
    job.publish_task_ids = {"task-1": "tiktok"}
    store = _make_store()
    artifacts = [_receipt_artifact("tiktok", "NOT_CONFIGURED", "missing token")]
    _handle_publish_result(store, job, {"success": True, "task_id": "task-1"}, artifacts, "tiktok")
    assert job.publish_task_ids == {}
    assert job.publication_results["tiktok"]["status"] == "NOT_CONFIGURED"
    assert job.publish_error == "missing token"
    assert job.status == JobStatus.PUBLISHING


def test_publish_result_failure_retries_up_to_max():
    job = _make_job()
    job.publish_task_ids = {"task-1": "youtube"}
    store = _make_store()
    enqueued = []

    def fake_enqueue(js, jb, ch):
        enqueued.append(ch)
        return {"task_id": "retry-1"}

    with patch("core.api.job_helpers._enqueue_publish_task", side_effect=fake_enqueue):
        for i in range(3):
            _handle_publish_result(store, job, {"success": False, "task_id": f"task-{i+1}", "error": "boom"}, [], "youtube")
    assert len(enqueued) == 3


def test_publish_result_failure_after_max_retries_marks_failed():
    job = _make_job(max_retries=1)
    job.publish_task_ids = {"task-1": "youtube"}
    store = _make_store()
    with patch("core.api.job_helpers._enqueue_publish_task") as mock_enq:
        for i in range(2):
            _handle_publish_result(store, job, {"success": False, "task_id": f"task-{i+1}", "error": "boom"}, [], "youtube")
    assert job.publish_attempt == 2
    assert len(mock_enq.call_args_list) == 1
    assert job.status == JobStatus.FAILED


def test_publish_result_dead_letter_records_failure_after_max_retries():
    """After max retries, publication_results has FAILED status for the channel."""
    job = _make_job(max_retries=0)
    job.publish_task_ids = {"task-1": "youtube"}
    store = _make_store()
    with patch("core.api.job_helpers._enqueue_publish_task"):
        _handle_publish_result(store, job, {"success": False, "task_id": "task-1", "error": "permanent"}, [], "youtube")
    assert job.publication_results.get("youtube", {}).get("status") == "FAILED"
    assert job.publication_results["youtube"]["error"] == "permanent"


def test_publish_local_stores_receipt_and_marks_published():
    job = _make_job(status=JobStatus.READY)
    store = _make_store()
    receipt = {"channel": "youtube", "status": "PUBLISHED", "id": "vid-1"}
    from unittest.mock import patch
    with patch("core.pipeline._do_publish_single", lambda j, ch: receipt):
        result = _publish_local(store, job, "youtube")
    assert result["status"] == "PUBLISHED"
    assert job.publication_results["youtube"]["status"] == "PUBLISHED"
    assert "youtube" in job.published_to


def test_publish_local_not_configured_sets_error():
    job = _make_job(status=JobStatus.READY)
    store = _make_store()
    receipt = {"channel": "youtube", "status": "NOT_CONFIGURED", "error": "no token"}
    from unittest.mock import patch
    with patch("core.pipeline._do_publish_single", lambda j, ch: receipt):
        result = _publish_local(store, job, "youtube")
    assert result["status"] == "NOT_CONFIGURED"
    assert job.publish_error == "no token"


def test_enqueue_publish_task_local_fallback_no_publisher(monkeypatch):
    from core.api import job_helpers as jh
    job = _make_job(status=JobStatus.READY)
    store = _make_store()
    store.workers = {}
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    called = {}

    def fake_publish_local(js, jb, ch):
        called["channel"] = ch
        return {"channel": ch, "status": "PUBLISHED"}

    monkeypatch.setattr(jh, "_publish_local", fake_publish_local)
    result = _enqueue_publish_task(store, job, "youtube")
    assert result is None
    assert called["channel"] == "youtube"


def test_enqueue_publish_task_dispatches_when_publisher_available(monkeypatch):
    import core.api.job_helpers as jh
    job = _make_job(status=JobStatus.PUBLISHING)
    store = _make_store()
    store.workers = {"pub-1": {"node_name": "pub-1", "role": "publisher"}}
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    result = _enqueue_publish_task(store, job, "youtube")
    assert result is not None
    assert result["task"] == "publish"
    assert "youtube" in job.publish_task_ids.values()


# ---------------------------------------------------------------------------
# Credential protection
# ---------------------------------------------------------------------------


SECRET_TOKEN = "sk-secret-token-12345"


def test_publish_receipt_does_not_leak_credentials(monkeypatch, tmp_path):
    """Publication receipts must never contain access tokens or secrets."""
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    fake = FakeTransport([httpx.Response(200, json={},
                                          headers={"Location": "https://u.example/1"}),
                          httpx.Response(200, json={"id": "vid-1"})])
    from publishers.youtube import YoutubePublisher
    publisher = YoutubePublisher(transport=fake)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    result = publisher.publish(str(video), {"topic": "test", "title": "Test"})
    assert result["status"] == "PUBLISHED"
    result_str = json.dumps(result)
    assert SECRET_TOKEN not in result_str
    assert "access_token" not in result_str.lower()
    assert "Bearer" not in result_str
