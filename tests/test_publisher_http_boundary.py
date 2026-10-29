"""#361: reject issued receipt mismatches before owner cleanup or queue ack."""
import base64
import copy
import hashlib
import json
import time

import httpx

import pytest

from core.state import store, task_queue
from tests import test_publisher_durable_reconciliation as setup
from tests.acceptance_provenance import acceptance_provenance
from worker.role_executor import execute_role_task


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_ROOT", str(tmp_path / "publisher-worker"))


@pytest.mark.parametrize("fault", ["job_id", "task_id", "video_version", "video_sha256", "channel", "missing", "foreign", "lease", "valid"])
def test_actual_publish_result_preserves_owner_until_valid(monkeypatch, tmp_path, fault):
    monkeypatch.setenv("NODE_API_TOKEN", setup._WORKER_TOKEN)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "boundary-test-token")
    data = setup._mp4_video(tmp_path)
    job = setup._new_job_with_video(data, hashlib.sha256(data).hexdigest())
    store.jobs[job.job_id] = job
    client = setup._make_client()
    setup._register_publisher_worker(client)
    assert client.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 200
    task = client.post("/api/tasks/claim", json={"node_name": "pub-worker", "vram_mb": 0}).json()["task"]
    setup._set_publisher_transport("youtube", setup._PLATFORMS[0][-1])
    [artifact] = execute_role_task("publisher", task)
    receipt = json.loads(base64.b64decode(artifact["data_base64"]))
    node = "pub-worker"
    if fault == "missing":
        receipt.pop("video_sha256")
    elif fault == "foreign":
        node = "foreign-worker"
    elif fault == "lease":
        task_queue.ack(task["task_id"])
    elif fault != "valid":
        receipt[fault] = "mismatch"
    artifact["data_base64"] = base64.b64encode(json.dumps(receipt).encode()).decode()
    before_worker = copy.deepcopy(store.workers["pub-worker"])
    before_results = copy.deepcopy(job.publication_results)
    before_tasks = dict(job.publish_task_ids)
    before_intent = copy.deepcopy(job.publish_intent)
    inflight = task_queue.inflight_has(task["task_id"])
    response = client.post("/api/tasks/result", json={"node_name": node, "job_id": job.job_id,
        "task_id": task["task_id"], "success": True, "artifacts": [artifact]})
    if fault == "valid":
        assert response.status_code == 200, response.text
        assert job.publication_results["youtube"]["status"] == "PUBLISHED"
    else:
        assert response.status_code in {400, 409}, response.text
        assert store.workers["pub-worker"] == before_worker
        assert job.publication_results == before_results
        assert job.publish_task_ids == before_tasks and job.publish_intent == before_intent
        assert task_queue.inflight_has(task["task_id"]) == inflight


@pytest.mark.parametrize("outcome", ["transient", "permanent", "not_configured"])
def test_actual_claim_time_backoff_exhaustion_and_permanent_failure(monkeypatch, tmp_path, outcome):
    monkeypatch.setenv("NODE_API_TOKEN", setup._WORKER_TOKEN)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    monkeypatch.setenv("PUBLISH_RETRY_BACKOFF_BASE", "1.5")
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "boundary-backoff-token" if outcome != "not_configured" else "")
    data = setup._mp4_video(tmp_path)
    job = setup._new_job_with_video(data, hashlib.sha256(data).hexdigest())
    job.max_retries = 1
    store.jobs[job.job_id] = job
    client = setup._make_client()
    setup._register_publisher_worker(client)
    assert client.post(f"/api/jobs/{job.job_id}/publish", json=["youtube"]).status_code == 200
    def take():
        return client.post("/api/tasks/claim", json={"node_name": "pub-worker", "vram_mb": 0}).json()["task"]
    task = take()
    setup._set_publisher_transport("youtube", [httpx.Response(503 if outcome == "transient" else 400, json={"error": "controlled failure"})])
    artifacts = execute_role_task("publisher", task)
    started = time.monotonic()
    response = client.post("/api/tasks/result", json={"node_name": "pub-worker", "job_id": job.job_id,
        "task_id": task["task_id"], "success": True, "artifacts": artifacts})
    assert response.status_code == 200, response.text
    assert take() is None
    if outcome != "transient":
        assert job.status.value == "FAILED" and task_queue.depth() == 0
        assert job.publish_retry_count.get("youtube", 0) == 0
        return
    retry = None
    while time.monotonic() - started < 4:
        retry = take()
        if retry:
            break
        time.sleep(.04)
    assert retry and time.monotonic() - started >= 1.4
    assert retry["task_id"] != task["task_id"]
    response = client.post("/api/tasks/result", json={"node_name": "pub-worker", "job_id": job.job_id,
        "task_id": retry["task_id"], "success": False, "error": "controlled transient exhaustion"})
    assert response.status_code == 200, response.text
    assert job.status.value == "FAILED" and job.publish_retry_count["youtube"] == 2
    assert take() is None and task_queue.depth() == 0
