import time
import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient
from core.app import app, store
from core.dispatcher import available_worker
from core.models import Job, JobStatus, utc_now

def test_health_and_job_flow():
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    response = client.post("/api/jobs", json={"topic": "Test topic"})
    assert response.status_code == 200
    assert response.json()["status"] in {"NEW", "SCRIPTING", "SCRIPT_READY", "ASSET_GENERATION", "ASSETS_READY", "ASSEMBLY", "READY"}
    job_id = response.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "READY":
            break
        time.sleep(0.025)
    assert job["status"] == "READY"
    video = client.get(f"/jobs/{job_id}/final/video.mp4")
    assert video.status_code == 200
    assert b"ftyp" in video.content[:32]


def test_raw_upload_registers_artifact(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Upload target"}).json()["job_id"]
    response = client.put(f"/api/jobs/{job_id}/uploads/references/reference.png",
                          content=b"\x89PNG\r\n\x1a\nplaceholder",
                          headers={"content-type": "image/png"})
    assert response.status_code == 200
    assert response.json()["sha256"]
    assert client.post(f"/api/jobs/{job_id}/artifacts/verify").json()["valid"] is True
    asset = next(item for item in client.get(f"/api/jobs/{job_id}/assets").json()
                 if item["artifact_id"] == response.json()["artifact_id"])
    assert asset["valid"] is True
    assert client.get(asset["url"]).status_code == 200
    artifact = next(item for item in store.jobs[job_id].artifacts if item.artifact_id == asset["artifact_id"])
    (store.root / job_id / artifact.path).write_bytes(b"tampered")
    assert client.get(asset["url"]).status_code == 409


def test_regenerate_preserves_inputs_and_replaces_generated_artifacts(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"topic": "Regeneration"}).json()["job_id"]
    for _ in range(200):
        before = client.get(f"/api/jobs/{job_id}").json()
        if before["status"] in {"READY", "FAILED"}:
            break
        time.sleep(0.025)
    uploaded = client.put(f"/api/jobs/{job_id}/uploads/references/input.png",
                          content=b"\x89PNG\r\n\x1a\ninput").json()
    old_generated = {item["artifact_id"] for item in before["artifacts"] if item["kind"] != "input"}

    regenerated = client.post(f"/api/jobs/{job_id}/regenerate").json()
    current_ids = {item["artifact_id"] for item in regenerated["artifacts"]}

    assert uploaded["artifact_id"] in current_ids
    assert not old_generated.intersection(current_ids)


def test_job_export_import_and_optimistic_lock(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")
    client = TestClient(app)
    created = client.post("/api/jobs", json={"topic": "Portable project"}).json()
    job_id = created["job_id"]
    for _ in range(200):
        current = client.get(f"/api/jobs/{job_id}").json()
        if current["status"] in {"READY", "FAILED"}:
            break
        time.sleep(0.025)
    stale = client.patch(f"/api/jobs/{job_id}", json={"expected_version": 1, "priority": 7})
    assert stale.status_code == 409
    archive = client.get(f"/api/jobs/{job_id}/export")
    assert archive.status_code == 200
    imported = client.post("/api/projects/import", content=archive.content,
                           headers={"content-type": "application/zip"})
    assert imported.status_code == 200
    assert imported.json()["job_id"] != job_id
    assert imported.json()["source"] == "import"

def test_telegram_deduplicates_and_worker_status(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "999")
    brand_root = tmp_path / "brands"
    brand_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRANDS_ROOT", str(brand_root))
    client = TestClient(app)
    brand_dir = brand_root / "brand01"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "brand.json").write_text(json.dumps({"id": "brand01", "name": "Test", "publishing": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
    update = {"message": {"message_id": 77, "text": "Telegram topic", "chat": {"id": 42}}}
    first = client.post("/api/telegram/webhook", json=update).json()
    assert first.get("status") == "brand_selection"
    assert "brands" in first
    callback = {"callback_query": {"id": "cb-1", "data": "select_brand:brand01",
                                    "message": {"chat": {"id": 42}, "message_id": 78}}}
    second = client.post("/api/telegram/webhook", json=callback).json()
    job_id = second.get("job_id") or second.get("job", {}).get("job_id")
    assert job_id
    third = client.post("/api/telegram/webhook", json=update).json()
    assert third.get("status") == "brand_selection"
    heartbeat = client.post("/api/workers/heartbeat", json={"node_name": "gpu-test", "gpu_name": "stub"})
    assert heartbeat.status_code == 200
    assert any(worker["node_name"] == "gpu-test" for worker in client.get("/api/workers").json())


def test_telegram_attachment_metadata_is_registered_as_input(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "999")
    brand_root = tmp_path / "brands"
    brand_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRANDS_ROOT", str(brand_root))
    client = TestClient(app)
    brand_dir = brand_root / "brand01"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "brand.json").write_text(json.dumps({"id": "brand01", "name": "Test", "publishing": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
    update = {"message": {"message_id": 78001, "chat": {"id": 42}, "caption": "Тема з фото",
                          "photo": [{"file_id": "photo-file"}]}}
    response = client.post("/api/telegram/webhook", json=update).json()
    assert response.get("status") == "brand_selection"
    callback = {"callback_query": {"id": "cb-1", "data": "select_brand:brand01",
                                    "message": {"chat": {"id": 42}, "message_id": 78002}}}
    job = client.post("/api/telegram/webhook", json=callback).json()
    inputs = [item for item in job.get("artifacts", []) if item.get("kind") == "input"]
    assert inputs and inputs[0]["filename"] == "telegram.json"
    assert inputs[0]["sha256"]
    client.post(f"/api/jobs/{job['job_id']}/cancel")

def test_dispatcher_respects_vram():
    job = Job(job_id="2026-999999", topic="image", character_id="did_samogon", priority=5,
              status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00", min_vram_mb=6000)
    worker = {"node_name": "gpu-01", "status": "ONLINE", "vram_mb": 6144,
              "last_seen": utc_now()}
    assert available_worker([worker], job)["node_name"] == "gpu-01"
    worker["vram_mb"] = 4096
    assert available_worker([worker], job) is None


def test_dispatcher_respects_worker_workflow_capability():
    job = Job(job_id="2026-999998", topic="image", character_id="did_samogon", priority=5,
              status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00",
              workflow="workflows/image/demo.json")
    worker = {"node_name": "gpu-01", "status": "ONLINE", "vram_mb": 6144,
              "last_seen": utc_now(), "supported_tasks": ["image"],
              "supported_workflows": ["workflows/image/custom.json"]}
    assert available_worker([worker], job) is None
    worker["supported_workflows"] = ["*"]
    assert available_worker([worker], job) is worker

def test_distributed_worker_result(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    heartbeat = client.post("/api/workers/heartbeat", json={
        "node_name": "gpu-real", "gpu_name": "GTX 1060", "vram_mb": 6144,
    })
    assert heartbeat.status_code == 200
    created = client.post("/api/jobs", json={"topic": "Distributed topic", "min_vram_mb": 128}).json()
    job_id = created["job_id"]
    task = None
    for _ in range(100):
        response = client.post("/api/tasks/claim", json={"node_name": "gpu-real", "gpu_name": "GTX 1060",
                                                          "vram_mb": 6144})
        task = response.json().get("task")
        if task and task["job_id"] == job_id:
            break
        time.sleep(0.01)
    assert task and task["job_id"] == job_id
    ppm = b"P6\n2 2\n255\n" + bytes((255, 0, 0)) * 4
    response = client.post("/api/tasks/result", json={"job_id": job_id, "node_name": "gpu-real",
                                                       "task_id": task["task_id"],
                                                       "success": True, "filename": "scene.ppm",
                                                       "image_base64": base64.b64encode(ppm).decode()})
    assert response.status_code == 200
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"READY", "FAILED"}:
            break
        time.sleep(0.025)
    assert job["status"] == "READY"

def test_admin_and_node_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("NODE_API_TOKEN", "node-secret")
    client = TestClient(app)
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", auth=("admin", "admin-secret")).status_code == 200
    payload = {"node_name": "secured-worker", "gpu_name": "test"}
    assert client.post("/api/workers/heartbeat", json=payload).status_code == 401
    assert client.post("/api/workers/heartbeat", json=payload,
                       headers={"X-Vertep-Token": "node-secret"}).status_code == 200
    assert client.get("/api/node/status/secured-worker",
                      headers={"X-Vertep-Token": "node-secret"}).status_code == 200
