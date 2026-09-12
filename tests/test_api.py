from unittest.mock import patch, MagicMock

import time
import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient
from core.app import app, store
from core.dispatcher import available_worker
from core.models import Job, JobStatus, StoryboardScene, StoryboardVersion, utc_now


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
from core.storyboard import StoryboardService as _StoryboardService

@pytest.fixture(autouse=True)
def _mock_storyboard_for_api(monkeypatch):
    monkeypatch.setattr(_StoryboardService, "queue", _mock_storyboard_queue)


def _approve_script_and_storyboard(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.025)
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


def _approve_storyboard(client, job_id):
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


def test_health_and_job_flow():
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    response = client.post("/api/jobs", json={"topic": "Test topic"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    assert job["status"] == "SCRIPT_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    _approve_storyboard(client, job_id)
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
    _approve_script_and_storyboard(client, job_id)
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
    _approve_script_and_storyboard(client, job_id)
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

def test_telegram_webhook_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_ENABLED", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "999")
    client = TestClient(app)
    response = client.post("/api/telegram/webhook", json={"message": {"message_id": 1, "text": "hi", "chat": {"id": 42}}})
    assert response.status_code == 404


def test_telegram_deduplicates_and_worker_status(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "999")
    brand_root = tmp_path / "brands"
    brand_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRANDS_ROOT", str(brand_root))
    character_root = tmp_path / "characters"
    character_root.mkdir(parents=True, exist_ok=True)
    char_dir = character_root / "did_samogon"
    char_dir_for_config = char_dir
    char_dir_for_characterize = char_dir
    char_dir_for_characterize.mkdir(parents=True, exist_ok=True)
    (char_dir_for_characterize / "character.json").write_text(json.dumps({
        "id": "did_samogon", "name": "Дід Самогонщик", "language": "uk", "enabled": True,
        "system_prompt": "Ти дід самогонщик.", "voice": {"provider": "none"},
        "visual": {"style": "documentary"}, "generation": {"workflow": "workflows/image/demo.json"},
        "publishing": {"enabled": False, "channels": []},
    }), encoding="utf-8")
    monkeypatch.setenv("CHARACTERS_ROOT", str(character_root))
    client = TestClient(app)
    brand_dir = brand_root / "brand01"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "brand.json").write_text(json.dumps({"id": "brand01", "name": "Test", "publishing": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"ok": True}
    with patch("adapters.telegram.httpx.post", return_value=mock_response):
        update = {"message": {"message_id": 77, "text": "Telegram topic", "chat": {"id": 42}}}
        first = client.post("/api/telegram/webhook", json=update).json()
        assert first.get("status") == "brand_selection"
        assert "brands" in first
        callback = {"callback_query": {"id": "cb-1", "data": "select_brand:brand01",
                                        "message": {"chat": {"id": 42}, "message_id": 78}}}
        second = client.post("/api/telegram/webhook", json=callback).json()
        assert second.get("status") == "character_selection"
        assert second.get("brand_id") == "brand01"
        character_callback = {"callback_query": {"id": "cb-2", "data": "select_character:did_samogon",
                                                  "message": {"chat": {"id": 42}, "message_id": 79}}}
        third = client.post("/api/telegram/webhook", json=character_callback).json()
        job_id = third.get("job_id") or third.get("job", {}).get("job_id")
        assert job_id
        fourth = client.post("/api/telegram/webhook", json=update).json()
        assert fourth.get("status") == "brand_selection"
    heartbeat = client.post("/api/workers/heartbeat", json={"node_name": "gpu-test", "gpu_name": "stub"})
    assert heartbeat.status_code == 200
    assert any(worker["node_name"] == "gpu-test" for worker in client.get("/api/workers").json())


def test_telegram_attachment_metadata_is_registered_as_input(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "999")
    brand_root = tmp_path / "brands"
    brand_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRANDS_ROOT", str(brand_root))
    character_root = tmp_path / "characters"
    character_root.mkdir(parents=True, exist_ok=True)
    char_dir = character_root / "did_samogon"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "character.json").write_text(json.dumps({
        "id": "did_samogon", "name": "Дід Самогонщик", "language": "uk", "enabled": True,
        "system_prompt": "Ти дід самогонщик.", "voice": {"provider": "none"},
        "visual": {"style": "documentary"}, "generation": {"workflow": "workflows/image/demo.json"},
        "publishing": {"enabled": False, "channels": []},
    }), encoding="utf-8")
    monkeypatch.setenv("CHARACTERS_ROOT", str(character_root))
    client = TestClient(app)
    brand_dir = brand_root / "brand01"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "brand.json").write_text(json.dumps({"id": "brand01", "name": "Test", "publishing": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
    update = {"message": {"message_id": 78001, "chat": {"id": 42}, "caption": "Тема з фото",
                          "photo": [{"file_id": "photo-file"}]}}
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"ok": True}
    with patch("adapters.telegram.httpx.post", return_value=mock_response):
        response = client.post("/api/telegram/webhook", json=update).json()
        assert response.get("status") == "brand_selection"
        callback = {"callback_query": {"id": "cb-1", "data": "select_brand:brand01",
                                        "message": {"chat": {"id": 42}, "message_id": 78002}}}
        brand_response = client.post("/api/telegram/webhook", json=callback).json()
        assert brand_response.get("status") == "character_selection"
        assert brand_response.get("brand_id") == "brand01"
        character_callback = {"callback_query": {"id": "cb-2", "data": "select_character:did_samogon",
                                                  "message": {"chat": {"id": 42}, "message_id": 78003}}}
        job = client.post("/api/telegram/webhook", json=character_callback).json()
        inputs = [item for item in job.get("artifacts", []) if item.get("kind") == "input"]
        assert inputs and inputs[0]["filename"] == "telegram.json"
        assert inputs[0]["sha256"]
        client.post(f"/api/jobs/{job['job_id']}/cancel")


def test_telegram_setup_saves_chat_ids_without_public_url(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "installation.json").write_text(json.dumps({"completed_at": "2024-01-01T00:00:00Z"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config_root))
    client = TestClient(app)
    response = client.post("/api/telegram/setup", json={
        "allowed_chat_ids": "111,222",
        "admin_chat_ids": "333",
        "webhook_secret": "supersecret",
    })
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "saved"
    assert "polling" in result["message"]
    status = client.get("/api/telegram/status").json()
    assert status["allowed_chat_ids"] == "111,222"
    assert status["admin_chat_ids"] == "333"
    assert status["webhook_secret_configured"] is True


def test_telegram_start_command_does_not_create_job(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    brand_root = tmp_path / "brands"
    brand_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRANDS_ROOT", str(brand_root))
    client = TestClient(app)
    brand_dir = brand_root / "brand01"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "brand.json").write_text(json.dumps({"id": "brand01", "name": "Test", "publishing": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
    update = {"message": {"message_id": 1, "text": "/start", "chat": {"id": 42}}}
    from unittest.mock import patch, MagicMock
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"ok": True}
    with patch("adapters.telegram.httpx.post", return_value=mock_response) as mock_post:
        response = client.post("/api/telegram/webhook", json=update).json()
    assert response.get("ok") is True


def test_telegram_polling_service_persists_offset(tmp_path, monkeypatch):
    from adapters.telegram import TelegramPollingService
    monkeypatch.setenv("TELEGRAM_POLLING_ENABLED", "true")
    state_file = tmp_path / "telegram_polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda update: None, offset_file=state_file)
    assert service.offset == 0
    service.offset = 123
    service.last_update_id = 123
    service.last_message_at = "2026-09-02T16:21:00+03:00"
    service._save_offset()
    assert state_file.exists(), f"state file missing: {state_file}"
    raw = state_file.read_text(encoding="utf-8")
    assert '"last_update_id": 123' in raw, raw
    loaded = TelegramPollingService(token="test-token", on_update=lambda update: None, offset_file=state_file)
    assert loaded.offset == 123
    assert loaded.last_update_id == 123
    assert loaded.last_message_at == "2026-09-02T16:21:00+03:00"


def test_telegram_polling_restart_continues_from_persisted_offset(tmp_path):
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "telegram_polling_state.json"
    processed = []
    service = TelegramPollingService(token="test-token", on_update=processed.append, offset_file=state_file)
    service.offset = 100
    service.last_update_id = 99
    service._save_offset()
    restarted = TelegramPollingService(token="test-token", on_update=processed.append, offset_file=state_file)
    assert restarted.offset == 100
    assert restarted.last_update_id == 99
    assert processed == []


def test_telegram_polling_offset_save_failure_does_not_crash(monkeypatch, tmp_path):
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "telegram_polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    service.offset = 42
    service.last_update_id = 41
    original_open = open
    def failing_open(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("builtins.open", failing_open)
    service._save_offset()
    assert service.offset == 42
    assert service.last_update_id == 41
    assert service._consecutive_failures == 0


def test_telegram_polling_backoff_on_error(tmp_path, monkeypatch):
    from adapters.telegram import TelegramPollingService
    monkeypatch.setenv("TELEGRAM_POLLING_RETRY_DELAY", "1")
    monkeypatch.setenv("TELEGRAM_POLLING_MAX_RETRY_DELAY", "10")
    state_file = tmp_path / "telegram_polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    monkeypatch.setattr(TelegramPollingService, "_delete_webhook", lambda self: None)
    call_count = [0]
    def failing_get_updates(self):
        call_count[0] += 1
        if call_count[0] >= 3:
            self._running = False
        raise OSError("network down")
    monkeypatch.setattr(TelegramPollingService, "_get_updates", failing_get_updates)
    delays = []
    monkeypatch.setattr("time.sleep", lambda s: delays.append(s))
    service._running = True
    service._run()
    assert call_count[0] == 3
    assert len(delays) == 2
    assert delays[0] == 1
    assert delays[1] == 2

def test_telegram_polling_handler_failure_does_not_advance_offset(tmp_path, monkeypatch):
    from adapters.telegram import TelegramPollingService
    monkeypatch.setenv("TELEGRAM_POLLING_RETRY_DELAY", "1")
    state_file = tmp_path / "telegram_polling_state.json"
    processed = []
    def failing_update(update):
        if update.get("update_id") == 100:
            raise RuntimeError("handler boom")
        processed.append(update["update_id"])
    service = TelegramPollingService(token="test-token", on_update=failing_update, offset_file=state_file)
    service.offset = 99
    monkeypatch.setattr(TelegramPollingService, "_delete_webhook", lambda self: None)
    calls = []
    def fake_get_updates(self):
        calls.append(self.offset)
        if len(calls) == 1:
            return [{"update_id": 100, "message": {"message_id": 1, "text": "boom", "chat": {"id": 42}}}]
        self._running = False
        return []
    monkeypatch.setattr(TelegramPollingService, "_get_updates", fake_get_updates)
    monkeypatch.setattr("time.sleep", lambda s: None)
    service._running = True
    service._run()
    assert service.offset == 99
    assert processed == []
    service._save_offset()
    reloaded = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    assert reloaded.offset == 99


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
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    assert job["status"] == "SCRIPT_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    _approve_storyboard(client, job_id)
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

def test_admin_and_node_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("NODE_API_TOKEN", "node-secret")
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "installation.json").write_text(json.dumps({"completed_at": "2024-01-01T00:00:00Z"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config_root))
    client = TestClient(app)
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", auth=("admin", "admin-secret")).status_code == 200
    payload = {"node_name": "secured-worker", "gpu_name": "test"}
    assert client.post("/api/workers/heartbeat", json=payload).status_code == 401
    assert client.post("/api/workers/heartbeat", json=payload,
                       headers={"X-Vertep-Token": "node-secret"}).status_code == 200
    assert client.get("/api/node/status/secured-worker",
                      headers={"X-Vertep-Token": "node-secret"}).status_code == 200
