import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from adapters.ffmpeg import FFmpegAdapter
from core.app import app, store
from adapters.llm import LLMAdapter


def wait_for(client, job_id, statuses=("READY", "FAILED")):
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.025)
    return job


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
    assert wait_for(client, job_id)["status"] == "READY"
    published = client.post(f"/api/jobs/{job_id}/publish", json=["youtube"]).json()
    assert published["status"] == "FAILED"
    assert published["publication_results"]["youtube"]["status"] == "NOT_CONFIGURED"
    assert published["stages"]["PUBLISH"]["status"] == "FAILED"
    assert len(published["stages"]["PUBLISH"]["attempts"]) == 1


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
    response = client.patch(f"/api/jobs/{job_id}", json={"workflow": "../../secret.json"})
    assert response.status_code == 400


def test_duplicate_worker_result_is_idempotent(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Idempotency", "min_vram_mb": 1}).json()["job_id"]
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
    monkeypatch.setattr(LLMAdapter, "generate_script", lambda self, topic, system_prompt="": {
        "title": topic, "scenes": [
            {"prompt": "first", "voiceover": "one", "duration": 1},
            {"prompt": "second", "voiceover": "two", "duration": 1},
        ]})
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Two scenes"}).json()["job_id"]
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
    job_id = client.post("/api/jobs", json={"topic": "Lease owner"}).json()["job_id"]
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
    job_id = client.post("/api/jobs", json={"topic": "Atomic artifacts"}).json()["job_id"]
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
    monkeypatch.setattr(LLMAdapter, "generate_script", lambda self, topic, system_prompt="": {
        "title": topic, "scenes": [{"prompt": "first", "duration": 1},
                                     {"prompt": "second", "duration": 1}]})
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Sibling cancellation"}).json()["job_id"]
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
    monkeypatch.setattr(LLMAdapter, "generate_script", lambda self, topic, system_prompt="": {
        "title": topic, "scenes": [{"prompt": "first", "duration": .2},
                                     {"prompt": "second", "duration": .2}]})
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Parallel fan-in"}).json()["job_id"]
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
    job_id = client.post("/api/jobs", json={"topic": "Cancel active", "min_vram_mb": 1}).json()["job_id"]
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
    response = client.post("/api/jobs", json={"topic": "Video worker contract", "task_type": "video",
                                                "workflow": "workflows/video/demo.json"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
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
