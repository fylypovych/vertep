import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from adapters.ffmpeg import FFmpegAdapter
from core.app import app, store
from core.models import JobStatus, StoryboardScene, StoryboardVersion
from core.script_agent import ScriptAgent
from core.storyboard import StoryboardService as _StoryboardServiceOrig
from worker import role_executor


def _complete_script_task(client, job_id, node_name="text-worker"):
    """Simulate a Text Worker claiming, executing, and submitting a script task."""
    client.post("/api/workers/heartbeat", json={"node_name": node_name, "vram_mb": 0,
                 "role": "text",
                 "capabilities": ["text_generation"], "supported_tasks": ["text"]})
    task = None
    for _ in range(100):
        resp = client.post("/api/tasks/claim", json={"node_name": node_name, "vram_mb": 0})
        task = resp.json().get("task")
        if task and task.get("task") == "script" and task.get("job_id") == job_id:
            break
        time.sleep(0.01)
    assert task and task.get("task") == "script"
    [artifact] = role_executor.execute_role_task("text", task)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200
    return artifact


def _mock_storyboard_queue(self, job, revision=None):
    target_store = getattr(self, "store", store)
    if not hasattr(job, "job_id"):
        job = target_store.jobs.get(job)
    if not job:
        raise ValueError("Job not found")
    script = job.script or {"title": job.topic, "scenes": [{"prompt": job.topic, "voiceover": "", "duration": 1}]}
    scenes = []
    for i, s in enumerate(script.get("scenes", []), 1):
        scenes.append(StoryboardScene(
            index=i, prompt=s.get("prompt", ""), video_prompt=s.get("prompt", ""),
            voiceover=s.get("voiceover", ""), duration=float(s.get("duration", 1)),
        ))
    storyboard = StoryboardVersion(
        version=(job.storyboards[-1].version + 1 if job.storyboards else 1),
        title=script.get("title", job.topic), description=script.get("description", ""),
        hashtags=script.get("hashtags", []), scenes=scenes, status="pending_approval",
        image_status="approved", image_version=1,
    )
    # Mock image previews as already approved so video generation is not blocked (Issue #6)
    for scene in storyboard.scenes:
        scene.scene_id = f"sb-{storyboard.version}-{scene.index}"
        scene.image_prompt = scene.prompt
        scene.image_version = 1
        scene.image_artifact_id = f"mock-art-{scene.index}"
        scene.artifact_id = scene.image_artifact_id
    job.storyboards.append(storyboard)
    job.active_storyboard_version = storyboard.version
    job.active_image_version = storyboard.image_version
    job.storyboard_error = None
    job.storyboard_task_id = "mock-task-id"
    target_store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL, f"STORYBOARD {storyboard.version} PENDING APPROVAL")
    return storyboard


import pytest


@pytest.fixture(autouse=True)
def _mock_storyboard_for_features(monkeypatch):
    monkeypatch.setattr(_StoryboardServiceOrig, "queue", _mock_storyboard_queue)


def wait_for(client, job_id, statuses=("READY", "FAILED")):
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.025)
    return job


def approve_script(client, job_id):
    local_fallback = os.getenv("LOCAL_WORKER_FALLBACK", "true").lower() == "true"
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if not local_fallback and job["status"] in ("SCRIPT_QUEUED", "SCRIPT_GENERATING"):
            _complete_script_task(client, job_id)
        if job["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.01)
    assert job["status"] == "SCRIPT_PENDING_APPROVAL"
    resp = client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    assert resp.status_code == 200
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "STORYBOARD_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    assert job["status"] == "STORYBOARD_PENDING_APPROVAL"
    storyboards = client.get(f"/api/jobs/{job_id}/storyboards").json()
    version = storyboards[-1]["version"] if storyboards else 1
    resp = client.post(f"/api/jobs/{job_id}/storyboards/approve", json={"version": version, "actor": "test"})
    assert resp.status_code == 200
    return resp.json()


def heartbeat(client, node_name, **overrides):
    payload = {"node_name": node_name, "vram_mb": 4096}
    payload.update(overrides)
    response = client.post("/api/workers/heartbeat", json=payload)
    assert response.status_code == 200


def test_multiscene_ffmpeg(tmp_path):
    images = []
    for index, color in enumerate(((255, 0, 0), (0, 255, 0))):
        path = tmp_path / f"scene-{index}.ppm"
        path.write_bytes(b"P6\n4 4\n255\n" + bytes(color) * 16)
        images.append(path)
    output = FFmpegAdapter().assemble(tmp_path / "video.mp4", images=images, durations=[0.2, 0.2], aspect_ratio="9:16")
    assert output.stat().st_size > 100
    assert b"ftyp" in output.read_bytes()[:32]


def test_ffmpeg_concatenates_video_clips(tmp_path):
    clips = []
    for index, color in enumerate(((120, 10, 20), (10, 120, 20))):
        image = tmp_path / f"clip-{index}.ppm"
        image.write_bytes(b"P6\n4 4\n255\n" + bytes(color) * 16)
        clips.append(FFmpegAdapter().assemble(tmp_path / f"clip-{index}.mp4", images=[image], durations=[0.2]))
    output = FFmpegAdapter().assemble_clips(tmp_path / "joined.mp4", clips)
    assert output.stat().st_size > 100
    assert b"ftyp" in output.read_bytes()[:32]


def test_platform_preset_and_watermark(tmp_path):
    scene = tmp_path / "scene.ppm"
    logo = tmp_path / "logo.ppm"
    scene.write_bytes(b"P6\n4 4\n255\n" + bytes((20, 30, 40)) * 16)
    logo.write_bytes(b"P6\n2 2\n255\n" + bytes((255, 255, 255)) * 4)
    output = FFmpegAdapter().assemble(tmp_path / "square.mp4", images=[scene], durations=[0.2],
                                      preset="square", watermark=logo)
    assert output.stat().st_size > 100


def test_publisher_never_fakes_success(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Publish test"}).json()["job_id"]
    approve_script(client, job_id)
    assert wait_for(client, job_id)["status"] == "READY"
    published = client.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).json()
    assert published["status"] == "FAILED"
    assert published["publication_results"]["youtube"]["status"] == "NOT_CONFIGURED"
    assert published["stages"]["PUBLISH"]["status"] == "FAILED"
    assert len(published["stages"]["PUBLISH"]["attempts"]) == 1


def _heartbeat_publisher(client, node_name="pub-worker"):
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": 0, "role": "publisher",
        "capabilities": ["publishing"], "supported_tasks": ["publish"]})


def _claim_publish_task(client, job_id, node_name="pub-worker"):
    for _ in range(100):
        resp = client.post("/api/tasks/claim", json={"node_name": node_name, "vram_mb": 0})
        task = resp.json().get("task")
        if task and task.get("task") == "publish" and task.get("job_id") == job_id:
            return task
        time.sleep(0.01)
    return None


def test_publish_via_publisher_worker_returns_receipt(monkeypatch):
    """Full publish flow: dispatch → claim → execute → result → receipt."""
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Publish via worker"}).json()["job_id"]
    approve_script(client, job_id)
    assert wait_for(client, job_id)["status"] == "READY"
    _heartbeat_publisher(client)
    published = client.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).json()
    assert published["status"] == "PUBLISHING"
    task = _claim_publish_task(client, job_id)
    assert task is not None
    [artifact] = role_executor.execute_role_task("publisher", task)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "pub-worker",
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "PUBLISHED"
    receipt = job["publication_results"]["youtube"]
    assert receipt["status"] == "PUBLISHED"
    assert receipt.get("id") or receipt.get("remote_id")
    assert receipt.get("url")
    assert job["published_to"] == ["youtube"]


def test_publish_partial_failure_mixed_channels(monkeypatch):
    """Publishing two channels: mock succeeds for youtube, worker fails for tiktok."""
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Partial publish"}).json()["job_id"]
    approve_script(client, job_id)
    assert wait_for(client, job_id)["status"] == "READY"
    _heartbeat_publisher(client)
    published = client.post(f"/api/jobs/{job_id}/publish", json=["youtube", "tiktok"]).json()
    assert published["status"] == "PUBLISHING"
    for _ in range(200):
        task = _claim_publish_task(client, job_id)
        if task is None:
            time.sleep(0.05)
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("PUBLISHED", "FAILED", "CANCELLED"):
                break
            continue
        if task["channel"] == "tiktok":
            client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": task["task_id"], "node_name": "pub-worker",
                "success": False, "artifacts": [], "error": "TikTok API error"})
        else:
            [artifact] = role_executor.execute_role_task("publisher", task)
            client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": task["task_id"], "node_name": "pub-worker",
                "success": True, "artifacts": [artifact]})
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["publication_results"]["youtube"]["status"] == "PUBLISHED"
    assert "youtube" in job["published_to"]
    assert job["status"] in ("FAILED", "PUBLISHING")


def test_publish_retry_on_worker_failure(monkeypatch):
    """Publish task failure triggers retry; succeeds on second attempt."""
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Retry publish"}).json()["job_id"]
    approve_script(client, job_id)
    assert wait_for(client, job_id)["status"] == "READY"
    _heartbeat_publisher(client)
    client.post(f"/api/jobs/{job_id}/publish", json=["youtube"])
    attempts = 0
    for _ in range(300):
        task = _claim_publish_task(client, job_id)
        if task is None:
            time.sleep(0.05)
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "PUBLISHED":
                break
            continue
        attempts += 1
        if attempts == 1:
            client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": task["task_id"], "node_name": "pub-worker",
                "success": False, "artifacts": [], "error": "transient network error"})
        else:
            [artifact] = role_executor.execute_role_task("publisher", task)
            client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": task["task_id"], "node_name": "pub-worker",
                "success": True, "artifacts": [artifact]})
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "PUBLISHED"
    assert job["publication_results"]["youtube"]["status"] == "PUBLISHED"
    assert attempts >= 2


def test_publish_cancels_pending_tasks_on_job_cancel(monkeypatch):
    """Cancelling a job in PUBLISHING discards publish tasks."""
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Cancel publish"}).json()["job_id"]
    approve_script(client, job_id)
    assert wait_for(client, job_id)["status"] == "READY"
    _heartbeat_publisher(client)
    published = client.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).json()
    assert published["status"] == "PUBLISHING"
    task = _claim_publish_task(client, job_id)
    if task:
        [artifact] = role_executor.execute_role_task("publisher", task)
        client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": task["task_id"], "node_name": "pub-worker",
            "success": True, "artifacts": [artifact]})
    else:
        client.post(f"/api/jobs/{job_id}/cancel")
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "CANCELLED"
        assert job["publish_task_ids"] == {}

        return


def _heartbeat_text_worker(client, node_name="text-worker"):
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": 0, "role": "text",
        "capabilities": ["text_generation"], "supported_tasks": ["text"]})


def test_script_generation_via_text_worker(monkeypatch):
    """CORE → Text Worker → result → SCRIPT_PENDING_APPROVAL (no LLM in CORE)."""
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    from core.script_agent import ScriptAgent
    monkeypatch.setattr(ScriptAgent, "generate_script",
                        lambda self, topic, sp="", ch=None: {
                            "title": topic,
                            "scenes": [{"prompt": topic, "voiceover": "", "duration": 1}]})
    client = TestClient(app)
    _heartbeat_text_worker(client)
    job_id = client.post("/api/jobs", json={"topic": "Script via worker"}).json()["job_id"]
    _complete_script_task(client, job_id)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "SCRIPT_PENDING_APPROVAL"


def test_script_task_failure_retries_then_fails():
    """Script task failure triggers retry; after max_retries, job → SCRIPT_FAILED."""
    from core.api.job_helpers import _handle_script_result
    from core.models import Job, JobStatus
    from unittest.mock import MagicMock, patch
    job = Job(job_id="job-fail", topic="fail", character_id="c", priority=5,
              status=JobStatus.SCRIPT_GENERATING, created_at="2024-01-01T00:00:00Z",
              script_task_id="task-1", script_attempt=0, max_retries=1, publish_task_ids={},
              publication_results={}, published_to=[])
    store = MagicMock()
    store.update = lambda j, status=None, msg="": setattr(j, "status", status)
    with patch("core.api.job_helpers._enqueue_script_task") as mock_enqueue, \
         patch("core.api.job_helpers._load_character_prompt", return_value=("", None)):
        _handle_script_result(store, job, {"success": False, "task_id": "task-1", "error": "LLM timeout"}, [])
        assert job.script_attempt == 1
        assert mock_enqueue.called
        _handle_script_result(store, job, {"success": False, "task_id": "task-2", "error": "LLM timeout again"}, [])
        assert job.status == JobStatus.SCRIPT_FAILED


def test_script_task_cancellation_via_api(monkeypatch):
    """Cancelling a job in SCRIPT_GENERATING discards the script task via API."""
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    from core.script_agent import ScriptAgent
    monkeypatch.setattr(ScriptAgent, "generate_script",
                        lambda self, topic, sp="", ch=None: {
                            "title": topic,
                            "scenes": [{"prompt": topic, "voiceover": "", "duration": 1}]})
    client = TestClient(app)
    _heartbeat_text_worker(client)
    job_id = client.post("/api/jobs", json={"topic": "Cancel script"}).json()["job_id"]
    job = wait_for(client, job_id, statuses=("SCRIPT_GENERATING", "SCRIPT_PENDING_APPROVAL"))
    if job["status"] == "SCRIPT_GENERATING":
        client.post(f"/api/jobs/{job_id}/cancel")
        updated = client.get(f"/api/jobs/{job_id}").json()
        assert updated["status"] == "CANCELLED"
        assert updated["script_task_id"] is None


def test_character_crud_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    client = TestClient(app)
    config = {"id": "tester", "name": "Tester", "language": "uk", "enabled": True,
              "system_prompt": "Prompt", "voice": {}, "visual": {"aspect_ratio": "9:16"},
              "generation": {"workflow": "workflows/image/demo.json"}, "publishing": {}}
    assert client.put("/api/characters/tester", json=config).status_code == 200
    assert client.get("/api/characters/tester").json()["visual"]["aspect_ratio"] == "9:16"
    invalid = dict(config, id="../escape")
    assert client.put("/api/characters/../escape", json=invalid).status_code >= 400


def test_job_rejects_workflow_outside_registry(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Workflow validation"}).json()["job_id"]
    approve_script(client, job_id)
    wait_for(client, job_id)
    response = client.patch(f"/api/jobs/{job_id}", json={"workflow": "../../secret.json"})
    assert response.status_code == 400


def test_duplicate_worker_result_is_idempotent(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    heartbeat(client, "idem-worker")
    job_id = client.post("/api/jobs", json={"topic": "Idempotency", "min_vram_mb": 1}).json()["job_id"]
    approve_script(client, job_id)
    task = None
    for _ in range(100):
        task = client.post("/api/tasks/claim", json={"node_name": "idem-worker", "vram_mb": 4096}).json().get("task")
        if task and task["job_id"] == job_id:
            break
        time.sleep(0.01)
    ppm = b"P6\n2 2\n255\n" + bytes((0, 0, 255)) * 4
    payload = {"job_id": job_id, "task_id": task["task_id"], "node_name": "idem-worker", "success": True,
               "filename": "scene.ppm", "image_base64": base64.b64encode(ppm).decode()}
    assert client.post("/api/tasks/result", json=payload).status_code == 200
    assert client.post("/api/tasks/result", json=payload).status_code == 200


def test_multiscene_job_fans_out_to_distinct_tasks(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setattr(ScriptAgent, "generate_script", lambda self, topic, system_prompt="", character=None: {
        "title": topic, "scenes": [
            {"prompt": "first", "voiceover": "one", "duration": 1},
            {"prompt": "second", "voiceover": "two", "duration": 1},
        ]})
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Two scenes"}).json()["job_id"]
    approve_script(client, job_id)
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if len(job.get("active_task_ids", {})) == 2:
            break
        time.sleep(0.01)
    assert sorted(job["active_task_ids"].values()) == ["scene-001", "scene-002"]
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200


def test_worker_cannot_submit_another_workers_task(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    heartbeat(client, "owner")
    job_id = client.post("/api/jobs", json={"topic": "Lease owner"}).json()["job_id"]
    approve_script(client, job_id)
    task = None
    for _ in range(100):
        task = client.post("/api/tasks/claim", json={"node_name": "owner", "vram_mb": 4096}).json().get("task")
        if task and task["job_id"] == job_id:
            break
        time.sleep(0.01)
    ppm = b"P6\n2 2\n255\n" + bytes((10, 20, 30)) * 4
    response = client.post("/api/tasks/result", json={"job_id": job_id, "task_id": task["task_id"],
                           "node_name": "intruder", "success": True, "filename": "scene.ppm",
                           "image_base64": base64.b64encode(ppm).decode()})
    assert response.status_code == 409
    client.post(f"/api/jobs/{job_id}/cancel")


def test_invalid_artifact_batch_is_not_partially_written(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    heartbeat(client, "atomic-worker")
    job_id = client.post("/api/jobs", json={"topic": "Atomic artifacts"}).json()["job_id"]
    approve_script(client, job_id)
    task = None
    for _ in range(100):
        task = client.post("/api/tasks/claim", json={"node_name": "atomic-worker", "vram_mb": 4096}).json().get("task")
        if task and task["job_id"] == job_id:
            break
        time.sleep(0.01)
    ppm = b"P6\n2 2\n255\n" + bytes((10, 20, 30)) * 4
    response = client.post("/api/tasks/result", json={"job_id": job_id, "task_id": task["task_id"],
                           "node_name": "atomic-worker", "success": True,
                           "images": [{"filename": "one.ppm", "image_base64": base64.b64encode(ppm).decode()},
                                      {"filename": "two.png", "image_base64": base64.b64encode(b"not-png").decode()}]})
    assert response.status_code == 400
    assert not list((store.root / job_id / "images").glob("scene-001*"))
    client.post(f"/api/jobs/{job_id}/cancel")


def test_exhausted_scene_cancels_parallel_sibling(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("MAX_RETRIES", "1")
    monkeypatch.setattr(ScriptAgent, "generate_script", lambda self, topic, system_prompt="", character=None: {
        "title": topic, "scenes": [{"prompt": "first", "duration": 1},
                                     {"prompt": "second", "duration": 1}]})
    client = TestClient(app)
    heartbeat(client, "fatal-worker")
    heartbeat(client, "sibling-worker")
    job_id = client.post("/api/jobs", json={"topic": "Sibling cancellation"}).json()["job_id"]
    approve_script(client, job_id)
    claimed = []
    for worker in ("fatal-worker", "sibling-worker"):
        for _ in range(100):
            task = client.post("/api/tasks/claim", json={"node_name": worker, "vram_mb": 4096}).json().get("task")
            if task and task["job_id"] == job_id:
                claimed.append((worker, task))
                break
            time.sleep(0.01)
    assert len(claimed) == 2
    worker, failed_task = claimed[0]
    failed = client.post("/api/tasks/result", json={"job_id": job_id, "task_id": failed_task["task_id"],
                                                    "node_name": worker, "success": False, "error": "fatal"})
    assert failed.json()["status"] == "FAILED"
    sibling_worker, sibling_task = claimed[1]
    cancellations = client.get(f"/api/tasks/cancellations/{sibling_worker}").json()
    assert any(item["task_id"] == sibling_task["task_id"] for item in cancellations)


def test_parallel_results_trigger_single_assembly(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setattr(ScriptAgent, "generate_script", lambda self, topic, system_prompt="", character=None: {
        "title": topic, "scenes": [{"prompt": "first", "duration": .2},
                                     {"prompt": "second", "duration": .2}]})
    client = TestClient(app)
    heartbeat(client, "parallel-a")
    heartbeat(client, "parallel-b")
    job_id = client.post("/api/jobs", json={"topic": "Parallel fan-in"}).json()["job_id"]
    approve_script(client, job_id)
    claimed = []
    for worker in ("parallel-a", "parallel-b"):
        for _ in range(100):
            task = client.post("/api/tasks/claim", json={"node_name": worker, "vram_mb": 4096}).json().get("task")
            if task and task["job_id"] == job_id:
                claimed.append((worker, task))
                break
            time.sleep(.01)
    assert len(claimed) == 2
    ppm = b"P6\n2 2\n255\n" + bytes((30, 40, 50)) * 4

    def submit(item):
        worker, task = item
        return client.post("/api/tasks/result", json={"job_id": job_id, "task_id": task["task_id"],
                           "node_name": worker, "success": True, "filename": "scene.ppm",
                           "image_base64": base64.b64encode(ppm).decode()}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, claimed)) == [200, 200]
    completed = wait_for(client, job_id)
    assert completed["status"] == "READY"
    assert sum("ASSEMBLY STARTED" in event for event in completed["events"]) == 1


def test_pause_requests_worker_cancellation(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    heartbeat(client, "cancel-worker")
    job_id = client.post("/api/jobs", json={"topic": "Cancel active", "min_vram_mb": 1}).json()["job_id"]
    approve_script(client, job_id)
    task = None
    for _ in range(100):
        task = client.post("/api/tasks/claim", json={"node_name": "cancel-worker", "vram_mb": 4096}).json().get("task")
        if task and task["job_id"] == job_id:
            break
        time.sleep(0.01)
    assert task
    renewed = client.post("/api/tasks/renew", json={"node_name": "cancel-worker", "task_id": task["task_id"]})
    assert renewed.json()["renewed"] is True
    assert client.post(f"/api/jobs/{job_id}/pause").json()["status"] == "PAUSED"
    cancellations = client.get("/api/tasks/cancellations/cancel-worker").json()
    assert cancellations[0]["task_id"] == task["task_id"]


def test_cookie_session_requires_csrf(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-admin-password-for-tests")
    client = TestClient(app)
    session = client.post("/api/session", auth=("admin", "strong-admin-password-for-tests"))
    assert session.status_code == 200
    csrf = client.cookies.get("vertep_csrf")
    client.auth = None
    assert client.post("/api/jobs", json={"topic": "No CSRF"}).status_code == 403
    response = client.post("/api/jobs", json={"topic": "With CSRF"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert client.delete("/api/session", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_distributed_video_artifact_reaches_final_assembly(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    image = tmp_path / "video-source.ppm"
    image.write_bytes(b"P6\n4 4\n255\n" + bytes((20, 40, 80)) * 16)
    clip = FFmpegAdapter().assemble(tmp_path / "worker-clip.mp4", images=[image], durations=[0.25])
    client = TestClient(app)
    heartbeat(client, "video-worker", capabilities=[], supported_tasks=["video"],
              supported_workflows=["*"])
    response = client.post("/api/jobs", json={"topic": "Video worker contract", "task_type": "video",
                                                "workflow": "workflows/video/demo.json"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    approve_script(client, job_id)
    task = None
    for _ in range(100):
        task = client.post("/api/tasks/claim", json={"node_name": "video-worker", "vram_mb": 4096,
                           "supported_tasks": ["video"], "supported_workflows": ["*"]}).json().get("task")
        if task and task["job_id"] == job_id:
            break
        time.sleep(0.01)
    assert task
    result = client.post("/api/tasks/result", json={"job_id": job_id, "task_id": task["task_id"],
                         "node_name": "video-worker", "success": True,
                         "artifacts": [{"filename": "scene.mp4", "kind": "video",
                                        "data_base64": base64.b64encode(clip.read_bytes()).decode()}]})
    assert result.status_code == 200
    completed = wait_for(client, job_id)
    assert completed["status"] == "READY"
    assert any(item["kind"] == "video_scene" for item in completed["artifacts"])
    assert b"ftyp" in client.get(f"/jobs/{job_id}/final/video.mp4").content[:32]


def test_tts_pipeline_routes_to_voice_worker_and_produces_audio(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("DEMO_MODE", "true")

    def _fake_assemble(self, output, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ftypmp42" + b"\x00" * 100)
        return output

    def _fake_probe(self, path):
        return {"format": "mp4", "validated": "container"}

    monkeypatch.setattr(FFmpegAdapter, "assemble", _fake_assemble)
    monkeypatch.setattr(FFmpegAdapter, "probe", _fake_probe)
    client = TestClient(app)
    heartbeat(client, "image-worker", supported_tasks=["image"], capabilities=["image_generation"])
    heartbeat(client, "voice-worker", supported_tasks=["voice"], capabilities=["speech_synthesis"], role="voice")
    job_id = client.post("/api/jobs", json={"topic": "TTS pipeline test",
                                            "character_id": "did_samogon"}).json()["job_id"]
    approve_script(client, job_id)
    image_task = None
    for _ in range(100):
        resp = client.post("/api/tasks/claim", json={"node_name": "image-worker", "vram_mb": 4096})
        body = resp.json()
        image_task = body.get("task")
        print(f"CLAIM iteration={_}, status={resp.status_code}, task_job={image_task.get('job_id') if image_task else None}, task_type={image_task.get('task') if image_task else None}")
        if image_task and image_task.get("job_id") == job_id:
            break
        time.sleep(0.01)
    print(f"FINAL image_task={image_task}")
    assert image_task
    ppm = b"P6\n2 2\n255\n" + bytes((0, 0, 255)) * 4
    image_result = client.post("/api/tasks/result", json={"job_id": job_id, "task_id": image_task["task_id"],
                               "node_name": "image-worker", "success": True,
                               "filename": "scene.ppm", "image_base64": base64.b64encode(ppm).decode()})
    assert image_result.status_code == 200
    tts_task = None
    for _ in range(100):
        tts_task = client.post("/api/tasks/claim", json={"node_name": "voice-worker", "vram_mb": 0}).json().get("task")
        if tts_task and tts_task["job_id"] == job_id:
            break
        time.sleep(0.01)
    assert tts_task
    assert tts_task.get("task") == "voice"
    assert tts_task.get("provider") == "none"
    assert tts_task.get("voice") is None
    import io as _io
    import math as _math
    import struct as _struct
    import wave as _wave
    _buf = _io.BytesIO()
    _w = _wave.open(_buf, "wb")
    _w.setnchannels(1); _w.setsampwidth(2); _w.setframerate(44100)
    _w.writeframes(b"".join(_struct.pack("<h", int(12000 * _math.sin(2 * _math.pi * 440 * i / 44100))) for i in range(44100))); _w.close()
    wav = _buf.getvalue()
    tts_result = client.post("/api/tasks/result", json={"job_id": job_id, "task_id": tts_task["task_id"],
                                 "node_name": "voice-worker", "success": True,
                                 "artifacts": [{"filename": "speech.wav", "data_base64": base64.b64encode(wav).decode()}]})
    assert tts_result.status_code == 200
    completed = wait_for(client, job_id)
    assert completed["status"] == "READY"
    audio_artifacts = [item for item in completed["artifacts"] if item["kind"] == "audio"]
    assert audio_artifacts
    audio_path = Path(store.root) / job_id / "audio" / audio_artifacts[0]["filename"]
    assert audio_path.exists()
    assert audio_path.read_bytes() == wav
