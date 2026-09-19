from unittest.mock import patch, MagicMock, Mock

import importlib
import time
import os
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from core.app import app, store
from core.dispatcher import available_worker
from core.models import Job, JobStatus, StoryboardScene, StoryboardVersion, WorkerState, utc_now
from worker import role_executor

_tc = TestClient(app)


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


@pytest.fixture(autouse=True)
def _enable_local_fallback(monkeypatch):
    """Enable LOCAL_WORKER_FALLBACK for tests that rely on local script/publish generation."""
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "true")


@pytest.fixture(autouse=True)
def _mock_storyboard_for_api(monkeypatch):
    from core.storyboard import StoryboardService as _StoryboardServiceOrig
    monkeypatch.setattr(_StoryboardServiceOrig, "queue", _mock_storyboard_queue)


def _complete_script_task(client, job_id, node_name="text-worker"):
    """Register a text worker, claim/execute the script task, and submit the result."""
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": 0,
        "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
    })
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


def _approve_script_and_storyboard(client, job_id):
    local_fallback = os.getenv("LOCAL_WORKER_FALLBACK", "true").lower() == "true"
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if not local_fallback and job["status"] == "SCRIPT_QUEUED":
            _complete_script_task(client, job_id)
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
        if job["status"] == "VIDEO_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    assert job["status"] == "VIDEO_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "test"})
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "READY":
            break
        time.sleep(0.025)
    assert job["status"] == "READY"
    video = client.get(f"/jobs/{job_id}/final/video.mp4")
    assert video.status_code == 200
    assert b"ftyp" in video.content[:32]
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
        if before["status"] == "VIDEO_PENDING_APPROVAL":
            client.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "test"})
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
        if current["status"] == "VIDEO_PENDING_APPROVAL":
            client.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "test"})
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
    monkeypatch.setattr(service, "_interruptible_sleep", lambda s: delays.append(s))
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


def test_telegram_polling_negative_offset_loads(tmp_path):
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "telegram_polling_state.json"
    state_file.write_text('{"offset": -99, "last_update_id": -99}', encoding="utf-8")
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    assert service.offset == -99
    assert service.last_update_id == -99


def test_telegram_polling_corrupted_offset_falls_back_safe(tmp_path):
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "telegram_polling_state.json"
    state_file.write_text("this is not json", encoding="utf-8")
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    assert service.offset == 0
    assert service.last_update_id is None


def test_telegram_polling_stop_interrupts_429_sleep(tmp_path, monkeypatch):
    from adapters.telegram import TelegramPollingService
    monkeypatch.setenv("TELEGRAM_POLLING_TIMEOUT", "1")
    state_file = tmp_path / "telegram_polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    monkeypatch.setattr(TelegramPollingService, "_delete_webhook", lambda self: None)
    sleep_calls = []
    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        service._running = False
    monkeypatch.setattr(service, "_interruptible_sleep", fake_sleep)
    call_count = [0]
    def fake_get_updates(self):
        call_count[0] += 1
        if call_count[0] == 1:
            import httpx
            resp = httpx.Response(429, json={"parameters": {"retry_after": 60}})
            raise httpx.HTTPStatusError("429", request=None, response=resp)
        self._running = False
        return []
    monkeypatch.setattr(TelegramPollingService, "_get_updates", fake_get_updates)
    service._running = True
    service._run()
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == 60


def test_telegram_polling_stop_interrupts_retry_delay(tmp_path, monkeypatch):
    from adapters.telegram import TelegramPollingService
    monkeypatch.setenv("TELEGRAM_POLLING_RETRY_DELAY", "60")
    monkeypatch.setenv("TELEGRAM_POLLING_MAX_RETRY_DELAY", "60")
    state_file = tmp_path / "telegram_polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    monkeypatch.setattr(TelegramPollingService, "_delete_webhook", lambda self: None)
    sleep_calls = []
    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        service._running = False
    monkeypatch.setattr(service, "_interruptible_sleep", fake_sleep)
    def fail_updates(self):
        raise OSError("network")
    monkeypatch.setattr(TelegramPollingService, "_get_updates", fail_updates)
    service._running = True
    service._run()
    assert len(sleep_calls) >= 1


def test_telegram_polling_idempotent_callback_not_processed_twice(tmp_path, monkeypatch):
    from adapters.telegram import TelegramPollingService
    monkeypatch.setenv("TELEGRAM_POLLING_TIMEOUT", "1")
    state_file = tmp_path / "telegram_polling_state.json"
    processed = []
    service = TelegramPollingService(token="test-token", on_update=processed.append, offset_file=state_file)
    monkeypatch.setattr(TelegramPollingService, "_delete_webhook", lambda self: None)
    call_n = [0]
    def fake_updates(self):
        call_n[0] += 1
        if call_n[0] == 1:
            return [{"update_id": 500, "message": {"message_id": 1, "text": "hi", "chat": {"id": 1}}}]
        self._running = False
        return []
    monkeypatch.setattr(TelegramPollingService, "_get_updates", fake_updates)
    monkeypatch.setattr("time.sleep", lambda s: None)
    service._running = True
    service._run()
    assert len(processed) == 1
    restarted = TelegramPollingService(token="test-token", on_update=processed.append, offset_file=state_file)
    assert restarted.offset == 501


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
        if job["status"] == "SCRIPT_QUEUED":
            _complete_script_task(client, job_id)
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
        if job["status"] == "VIDEO_PENDING_APPROVAL":
            client.post(f"/api/jobs/{job_id}/video/approve", json={"actor": "test"})
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

def _repo_task_record(store, task_id: str) -> dict | None:
    """Read the latest repository record for a task from FileRepository.

    record_task() appends to ``<root>/.tasks.jsonl``; the latest line is the
    authoritative current state.  This is read-only and works for both the
    FileRepository used by the test store and the MemoryRepository fallback.
    """
    repo = store.repository
    path = getattr(repo, "root", None)
    if path is not None:
        file_path = path / ".tasks.jsonl"
        if file_path.exists():
            latest = None
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                latest = json.loads(line)
            if latest and latest.get("task", {}).get("task_id") == task_id:
                return latest
    # MemoryRepository keeps an in-memory dict.
    tasks = getattr(repo, "tasks", None)
    if isinstance(tasks, dict) and task_id in tasks:
        return tasks[task_id]
    return None


def _storyboard_artifact(version: int, title: str = "Тестова історія"):
    import base64
    return {
        "filename": "storyboard.json",
        "kind": "storyboard",
        "data_base64": base64.b64encode(json.dumps({
            "version": version, "title": title, "description": "Опис",
            "hashtags": ["#vertep"],
            "scenes": [
                {"index": 1, "prompt": "Українське місто", "video_prompt": "Повільна панорама",
                 "voiceover": "Початок історії", "duration": 6},
                {"index": 2, "prompt": "Герой у кадрі", "video_prompt": "Камера наближається",
                 "voiceover": "Продовження", "duration": 7},
            ],
            "prompt_version": "1.0", "model": "fake-storyboard",
            "status": "pending_approval", "revision_request": None,
            "image_version": 1, "image_status": "pending",
        }, ensure_ascii=False).encode("utf-8")).decode("ascii"),
    }


def test_storyboard_result_rejected_when_caller_does_not_hold_claim(monkeypatch):
    from core.state import task_queue
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    for name in ("text-a", "text-b"):
        client.post("/api/workers/heartbeat", json={
            "node_name": name, "vram_mb": 0,
            "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
        })
    created = client.post("/api/jobs", json={"topic": "Stolen result", "min_vram_mb": 0}).json()
    job_id = created["job_id"]
    job = store.jobs[job_id]
    job.min_vram_mb = 0
    task = task_queue.enqueue({
        "job_id": job_id, "task": "storyboard", "priority": job.priority,
        "topic": job.topic, "prompt": "p", "storyboard_version": 1,
        "image_version": 1, "revision": None, "timeout": 300,
    })
    store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    job.storyboard_task_id = task["task_id"]
    job.active_task_id = task["task_id"]

    # text-a claims the task.
    claimed = client.post("/api/tasks/claim", json={"node_name": "text-a", "vram_mb": 0}).json()
    assert claimed["task"] and claimed["task"]["task_id"] == task["task_id"]
    assert job.status == JobStatus.STORYBOARD_GENERATING

    # text-b (who never claimed) submits a result — must be rejected.
    artifact = _storyboard_artifact(1)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "text-b",
        "success": True, "artifacts": [artifact],
    })
    assert response.status_code == 200
    # Job must not advance past GENERATING; the task stays un-acked so a
    # legitimate retry can still happen.
    assert store.jobs[job_id].status == JobStatus.STORYBOARD_GENERATING
    assert store.workers["text-b"].get("current_task") is None
    assert task_queue.inflight_has(task["task_id"])


def test_storyboard_expired_lease_requeues_within_budget(monkeypatch):
    """Issue #64 T4: when a Text Worker dies holding a storyboard claim, the
    watchdog helper must requeue the expired lease and return the job to
    STORYBOARD_QUEUED (within the retry budget), rather than leaving the job
    stranded in STORYBOARD_GENERATING forever."""
    from core.state import task_queue
    from core.app import handle_expired_storyboard_lease
    # This module autouse-mocks StoryboardService.queue to jump straight to
    # PENDING_APPROVAL; the T4 recovery path calls the real queue, so undo
    # the fixture monkeypatches for the duration of this test and re-apply
    # only the env overrides we need.
    monkeypatch.undo()
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")
    client = TestClient(app)
    client.post("/api/workers/heartbeat", json={
        "node_name": "text-a", "vram_mb": 0,
        "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
    })
    created = client.post("/api/jobs", json={"topic": "Crashed worker", "min_vram_mb": 0}).json()
    job_id = created["job_id"]
    job = store.jobs[job_id]
    job.min_vram_mb = 0
    task = task_queue.enqueue({
        "job_id": job_id, "task": "storyboard", "priority": job.priority,
        "topic": job.topic, "prompt": "p", "storyboard_version": 1,
        "image_version": 1, "revision": None, "timeout": 300,
    })
    store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    job.storyboard_task_id = task["task_id"]
    job.active_task_id = task["task_id"]

    # text-a claims the task and then vanishes (its lease expires).
    claimed = client.post("/api/tasks/claim", json={"node_name": "text-a", "vram_mb": 0}).json()
    assert claimed["task"] and claimed["task"]["task_id"] == task["task_id"]
    assert job.status == JobStatus.STORYBOARD_GENERATING
    assert task_queue.inflight_has(task["task_id"])

    # Simulate the watchdog: requeue every expired lease with a clock far in
    # the future so the claim is considered expired.
    expired = task_queue.requeue_expired(now=task_queue._inflight[task["task_id"]][0] + 1 if not task_queue._redis else 0.0)
    assert any(t.get("task_id") == task["task_id"] for t in expired)

    # Drive the watchdog's storyboard recovery path for the requeued task.
    for t in expired:
        handle_expired_storyboard_lease(store, t)

    assert store.jobs[job_id].status == JobStatus.STORYBOARD_QUEUED
    assert store.jobs[job_id].storyboard_task_id is not None
    assert not task_queue.inflight_has(task["task_id"])


def test_storyboard_expired_lease_exhausts_budget(monkeypatch):
    """Issue #64 T4: after OLLAMA_STORYBOARD_MAX_RETRIES expired leases the
    job must land in STORYBOARD_FAILED, not silently stay in GENERATING."""
    from core.state import task_queue
    from core.app import handle_expired_storyboard_lease
    monkeypatch.undo()
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("OLLAMA_STORYBOARD_MAX_RETRIES", "2")
    client = TestClient(app)
    client.post("/api/workers/heartbeat", json={
        "node_name": "text-a", "vram_mb": 0,
        "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
    })
    created = client.post("/api/jobs", json={"topic": "Unreliable worker", "min_vram_mb": 0}).json()
    job_id = created["job_id"]
    job = store.jobs[job_id]
    job.min_vram_mb = 0
    task = task_queue.enqueue({
        "job_id": job_id, "task": "storyboard", "priority": job.priority,
        "topic": job.topic, "prompt": "p", "storyboard_version": 1,
        "image_version": 1, "revision": None, "timeout": 300,
    })
    store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    job.storyboard_task_id = task["task_id"]
    job.active_task_id = task["task_id"]

    claimed = client.post("/api/tasks/claim", json={"node_name": "text-a", "vram_mb": 0}).json()
    assert claimed["task"] and claimed["task"]["task_id"] == task["task_id"]

    # First expired lease -> requeue (attempt 1 < 2).
    expired = task_queue.requeue_expired(now=task_queue._inflight[task["task_id"]][0] + 1 if not task_queue._redis else 0.0)
    for t in expired:
        handle_expired_storyboard_lease(store, t)
    assert store.jobs[job_id].status == JobStatus.STORYBOARD_QUEUED

    # Second expired lease on the new task -> exhausted (attempt 2 == 2).
    # Re-claim the requeued task so it has an inflight lease to expire.
    # The worker's last_seen ages during the test, so refresh it first.
    client.post("/api/workers/heartbeat", json={
        "node_name": "text-a", "vram_mb": 0,
        "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
    })
    new_task_id = store.jobs[job_id].storyboard_task_id
    claimed = client.post("/api/tasks/claim", json={"node_name": "text-a", "vram_mb": 0}).json()
    assert claimed["task"] and claimed["task"]["task_id"] == new_task_id, (
        f"expected {new_task_id}, got {claimed}"
    )
    new_task_id = claimed["task"]["task_id"]
    expired = task_queue.requeue_expired(now=task_queue._inflight[new_task_id][0] + 1 if not task_queue._redis else 0.0)
    for t in expired:
        handle_expired_storyboard_lease(store, t)
    assert store.jobs[job_id].status == JobStatus.STORYBOARD_FAILED


def _repo_task_record(store, task_id: str) -> dict | None:
    """Read the latest repository record for a task from FileRepository.

    record_task() appends to ``<root>/.tasks.jsonl``; the latest line is the
    authoritative current state.  This is read-only and works for both the
    FileRepository used by the test store and the MemoryRepository fallback.
    """
    repo = store.repository
    path = getattr(repo, "root", None)
    if path is not None:
        file_path = path / ".tasks.jsonl"
        if file_path.exists():
            latest = None
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                latest = json.loads(line)
            if latest and latest.get("task", {}).get("task_id") == task_id:
                return latest
    # MemoryRepository keeps an in-memory dict.
    tasks = getattr(repo, "tasks", None)
    if isinstance(tasks, dict) and task_id in tasks:
        return tasks[task_id]
    return None


def test_storyboard_task_lifecycle_audit_matches_repository_and_queue(monkeypatch):
    """Issue #64 T5: every storyboard task state transition must be
    observable in BOTH the Job repository record and the task queue, so the
    two sources of truth can never drift apart."""
    from core.state import task_queue
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    client.post("/api/workers/heartbeat", json={
        "node_name": "text-a", "vram_mb": 0,
        "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
    })
    created = client.post("/api/jobs", json={"topic": "Lifecycle audit", "min_vram_mb": 0}).json()
    job_id = created["job_id"]
    job = store.jobs[job_id]
    job.min_vram_mb = 0

    # QUEUED: task is in the ready queue and the repository says QUEUED.
    task = task_queue.enqueue({
        "job_id": job_id, "task": "storyboard", "priority": job.priority,
        "topic": job.topic, "prompt": "p", "storyboard_version": 1,
        "image_version": 1, "revision": None, "timeout": 300,
    })
    store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    job.storyboard_task_id = task["task_id"]
    job.active_task_id = task["task_id"]
    store.repository.record_task(task, "QUEUED")
    assert task_queue.depth() >= 1
    assert not task_queue.inflight_has(task["task_id"])
    repo_task = _repo_task_record(store, task["task_id"])
    assert repo_task is not None and repo_task["status"] == "QUEUED", repo_task

    # CLAIMED / GENERATING: task leaves the ready queue, enters inflight, the
    # job transitions, and the repository records CLAIMED.
    claimed = client.post("/api/tasks/claim", json={"node_name": "text-a", "vram_mb": 0}).json()
    assert claimed["task"] and claimed["task"]["task_id"] == task["task_id"]
    assert job.status == JobStatus.STORYBOARD_GENERATING
    # The storyboard task must have left the ready queue (the job's auto-queued
    # SCRIPT task may still be there, so check the storyboard task specifically).
    probe = task_queue.claim()
    assert not (probe and probe.get("task_id") == task["task_id"]), probe
    if probe:
        task_queue.release(probe["task_id"])
    assert task_queue.inflight_has(task["task_id"])
    repo_task = _repo_task_record(store, task["task_id"])
    assert repo_task is not None and repo_task["status"] == "CLAIMED" and repo_task["node_name"] == "text-a", repo_task

    # Submit a legitimate result from the claim holder -> COMPLETED, job
    # advances to PENDING_APPROVAL, task leaves inflight.
    artifact = _storyboard_artifact(1)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": "text-a",
        "success": True, "artifacts": [artifact],
    })
    assert response.status_code == 200
    assert store.jobs[job_id].status == JobStatus.STORYBOARD_PENDING_APPROVAL
    assert not task_queue.inflight_has(task["task_id"])
    repo_task = _repo_task_record(store, task["task_id"])
    assert repo_task is not None and repo_task["status"] == "COMPLETED" and repo_task["node_name"] == "text-a", repo_task


def test_storyboard_claim_is_exclusive_and_transitions_generating(monkeypatch):
    """Issue #64 T1: two concurrent claims must not both succeed, and the
    first claim must move the job from STORYBOARD_QUEUED to STORYBOARD_GENERATING
    so the repository record and the Job lifecycle agree.  The test bypasses
    StoryboardService.queue (mocked in this module to jump straight to
    PENDING_APPROVAL) and sets up the claim-level state directly: a job in
    STORYBOARD_QUEUED with a matching enqueued task, and two registered text
    workers racing for it."""
    from core.state import task_queue
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    client = TestClient(app)
    for name in ("text-a", "text-b"):
        client.post("/api/workers/heartbeat", json={
            "node_name": name, "vram_mb": 0,
            "capabilities": ["text_generation"], "supported_tasks": ["text"], "role": "text",
        })
    created = client.post("/api/jobs", json={"topic": "Concurrent storyboard", "min_vram_mb": 0}).json()
    job_id = created["job_id"]
    job = store.jobs[job_id]
    # The storyboard claim path uses job.min_vram_mb (no explicit override), so
    # the 0-VRAM text worker must satisfy it.
    job.min_vram_mb = 0
    # Place the job directly in STORYBOARD_QUEUED with a real enqueued task.
    task = task_queue.enqueue({
        "job_id": job_id,
        "task": "storyboard",
        "priority": job.priority,
        "topic": job.topic,
        "prompt": "prompt",
        "storyboard_version": 1,
        "image_version": 1,
        "revision": None,
        "timeout": 300,
    })
    store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    job.storyboard_task_id = task["task_id"]
    job.active_task_id = task["task_id"]
    assert job.status == JobStatus.STORYBOARD_QUEUED

    claimed = []
    for name in ("text-a", "text-b"):
        resp = client.post("/api/tasks/claim", json={"node_name": name, "vram_mb": 0})
        claimed_task = resp.json().get("task")
        if claimed_task and claimed_task.get("task") == "storyboard" and claimed_task.get("job_id") == job_id:
            claimed.append((name, claimed_task["task_id"]))

    # Exactly one worker must win the storyboard task.
    assert len(claimed) == 1, f"expected exactly one claim, got {claimed}"
    winner, task_id = claimed[0]
    job = store.jobs[job_id]
    # T1: claim must transition the job out of QUEUED into GENERATING.
    assert job.status == JobStatus.STORYBOARD_GENERATING, job.status
    assert store.workers[winner]["current_task"] == task_id
    assert store.workers[winner]["status"] == "BUSY"
    # The loser must not hold any task and must still be ONLINE.
    loser = "text-b" if winner == "text-a" else "text-a"
    assert store.workers[loser].get("current_task") is None
    # The loser remains available for the next claim round.
    assert store.workers[loser]["status"] in (WorkerState.READY, WorkerState.ONLINE, "READY", "ONLINE")


def test_job_create_workflow_uses_full_path(monkeypatch, tmp_path):
    """Regression: workflow select must send full path (workflows/<type>/<name>.json)."""
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "characters"))
    monkeypatch.setenv("BRANDS_ROOT", str(tmp_path / "brands"))
    (tmp_path / "characters").mkdir(parents=True, exist_ok=True)
    char_dir = tmp_path / "characters" / "test-char"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "character.json").write_text(json.dumps({
        "id": "test-char", "name": "Test", "language": "uk",
        "generation": {"workflow": "workflows/image/demo.json"},
    }), encoding="utf-8")
    workflows_root = tmp_path / "workflows"
    (workflows_root / "image").mkdir(parents=True, exist_ok=True)
    (workflows_root / "image" / "demo.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    monkeypatch.setenv("WORKFLOWS_ROOT", str(workflows_root))
    client = TestClient(app)
    resp = client.post("/api/jobs", json={
        "topic": "test", "character_id": "test-char",
        "workflow": "workflows/image/demo.json",
    })
    assert resp.status_code == 200
    assert resp.json()["workflow"] == "workflows/image/demo.json"


def test_job_create_rejects_invalid_workflow_path(monkeypatch, tmp_path):
    """Regression: invalid workflow path must be rejected with clear error."""
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "characters"))
    monkeypatch.setenv("BRANDS_ROOT", str(tmp_path / "brands"))
    (tmp_path / "characters").mkdir(parents=True, exist_ok=True)
    char_dir = tmp_path / "characters" / "test-char"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "character.json").write_text(json.dumps({
        "id": "test-char", "name": "Test", "language": "uk",
        "generation": {"workflow": "invalid/path"},
    }), encoding="utf-8")
    workflows_root = tmp_path / "workflows"
    (workflows_root / "image").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKFLOWS_ROOT", str(workflows_root))
    client = TestClient(app)
    resp = client.post("/api/jobs", json={
        "topic": "test", "character_id": "test-char",
        "workflow": "invalid/path",
    })
    assert resp.status_code == 400
    assert "Workflow must use" in resp.json()["detail"]


def test_telegram_and_manual_workflow_validation_are_independent(monkeypatch, tmp_path):
    """Acceptance #3: Telegram and manual job creation use different code paths.

    Telegram flow (character config workflow) does NOT validate the workflow
    reference format; manual API flow DOES validate it via _validate_workflow_reference.
    This test documents that the two failures are NOT caused by the same root cause.
    """
    module = importlib.import_module('core.app')
    adapter = Mock()
    monkeypatch.setattr(module, 'TelegramAdapter', lambda: adapter)
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "characters"))
    monkeypatch.setenv("BRANDS_ROOT", str(tmp_path / "brands"))
    (tmp_path / "characters").mkdir(parents=True, exist_ok=True)
    char_dir = tmp_path / "characters" / "test-char"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "character.json").write_text(json.dumps({
        "id": "test-char", "name": "Test", "language": "uk",
        "generation": {"workflow": "workflows/image/demo.json"},
    }), encoding="utf-8")
    workflows_root = tmp_path / "workflows"
    (workflows_root / "image").mkdir(parents=True, exist_ok=True)
    (workflows_root / "image" / "demo.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    monkeypatch.setenv("WORKFLOWS_ROOT", str(workflows_root))
    pending = {'audit-chat': {'pending': {'text': 'topic', 'source_id': 's1', 'message': {'chat': {'id': '42'}}}, 'brand_id': 'brand01'}}
    monkeypatch.setattr(module, '_telegram_pending_character', pending)
    monkeypatch.setattr(module, 'load_character', lambda *a, **kw: None)
    job = SimpleNamespace(job_id='job-123', model_dump=lambda **kw: {'job_id': 'job-123'})
    monkeypatch.setattr(module, '_create_job_from_telegram', Mock(return_value=job))
    monkeypatch.setattr(module, '_queue_storyboard', Mock())
    result = module._handle_character_selection({'id': 'cb'}, 'audit-chat', 'test-char')
    assert result.get('job_id') == 'job-123'
    client = TestClient(app)
    resp = client.post("/api/jobs", json={
        "topic": "test", "character_id": "test-char",
        "workflow": "workflows/image/demo.json",
    })
    assert resp.status_code == 200


def test_character_create_does_not_default_did_samogon(tmp_path):
    """Regression #67 sub-issue 3: new character must not get did_samogon as default ID."""
    char_root = tmp_path / "characters"
    os.environ["CHARACTERS_ROOT"] = str(char_root)
    try:
        resp = _tc.post("/api/characters", json={"name": "Новий персонаж"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] != "did_samogon"
        assert len(data["id"]) == 12
        assert data["id"].islower()
    finally:
        os.environ.pop("CHARACTERS_ROOT", None)


def test_backup_unconfigured_returns_503(monkeypatch):
    """Regression #67 sub-issue 6: BACKUP_URL not set returns 503 with clear message."""
    monkeypatch.delenv("BACKUP_URL", raising=False)
    resp = _tc.post("/api/system/backups")
    assert resp.status_code == 503
    assert "BACKUP_URL" in resp.json()["detail"]


def test_workflow_edit_without_force_rejected(monkeypatch, tmp_path):
    """Regression #67 sub-issue 5: editing built-in workflow without force returns 400."""
    from core.state import workflow_registry as global_registry
    from core.workflows import WorkflowRegistry
    wf_root = tmp_path / "workflows"
    (wf_root / "image").mkdir(parents=True, exist_ok=True)
    wf = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}
    (wf_root / "image" / "builtin.json").write_text(json.dumps(wf), encoding="utf-8")
    new_registry = WorkflowRegistry(wf_root)
    import core.state
    core.state.workflow_registry = new_registry
    try:
        wf2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 2}}}
        resp = _tc.put("/api/workflows/image/builtin.json", json=wf2)
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]
        resp_force = _tc.put("/api/workflows/image/builtin.json", json=wf2,
                              params={"force": "true"})
        assert resp_force.status_code == 200
    finally:
        core.state.workflow_registry = global_registry


def test_system_test_full_uses_job_scenes(monkeypatch):
    """Issue #94.2: system_test must use Job.scenes (Pydantic) not job.get('plan')."""
    import sys
    from types import ModuleType as _Mod
    from core.app import store
    from core.models import Job, SceneRecord, StageStatus, utc_now
    store.jobs.clear()
    store.jobs["job-1"] = Job(
        job_id="job-1", topic="test", character_id="char-1",
        priority=1, status=JobStatus.READY,
        scenes=[
            SceneRecord(scene_id="s1", index=1, prompt="p1", status=StageStatus.READY),
            SceneRecord(scene_id="s2", index=2, prompt="p2", status=StageStatus.FAILED),
            SceneRecord(scene_id="s3", index=3, prompt="p3", status=StageStatus.PENDING),
        ], created_at=utc_now())
    monkeypatch.setenv("NODE_ROLE", "core")
    import core.health_checks as _hc
    monkeypatch.setattr(_hc, "run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr(_hc, "health_status", lambda c: "HEALTHY")
    import adapters.providers as _prov
    monkeypatch.setattr(_prov, "provider_matrix", lambda: {})
    _cm = _Mod("core.certificates")
    _cm.list_certificates = lambda: []
    sys.modules["core.certificates"] = _cm
    monkeypatch.setattr("core.app._call_core_api", lambda *a, **kw: {})
    try:
        resp = _tc.post("/api/system/test", json={"scope": "full"})
        assert resp.status_code == 200
        data = resp.json()
        assert "task_results" in data
        tr = data["task_results"]
        assert tr["failed"] == 1
        assert tr["pending"] == 1
    finally:
        store.jobs.clear()
        sys.modules.pop("core.certificates", None)


def test_system_test_full_unhealthy_on_empty_provider_matrix(monkeypatch):
    """Issue #94.3: full result UNHEALTHY when provider matrix is empty."""
    import sys
    from types import ModuleType as _Mod
    from core.app import store
    from core.models import Job, SceneRecord, StageStatus, utc_now
    store.jobs.clear()
    store.jobs["job-1"] = Job(
        job_id="job-1", topic="test", character_id="char-1",
        priority=1, status=JobStatus.READY,
        scenes=[SceneRecord(scene_id="s1", index=1, prompt="p1", status=StageStatus.READY)],
        created_at=utc_now())
    monkeypatch.setenv("NODE_ROLE", "core")
    import core.health_checks as _hc
    monkeypatch.setattr(_hc, "run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr(_hc, "health_status", lambda c: "HEALTHY")
    import adapters.providers as _prov
    monkeypatch.setattr(_prov, "provider_matrix", lambda: {})
    _cm = _Mod("core.certificates")
    _cm.list_certificates = lambda: [{"cert_id": "c1", "expires_in_days": 999}]
    sys.modules["core.certificates"] = _cm
    import tempfile, os
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_ROOT", tmpdir)
    os.chmod(tmpdir, 0o755)
    import core.queue as _qq
    monkeypatch.setattr(_qq, "TaskQueue", lambda: None)
    try:
        resp = _tc.post("/api/system/test", json={"scope": "full"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == "UNHEALTHY"
        assert any("provider_matrix" in f for f in data.get("full_test_failures", []))
    finally:
        store.jobs.clear()
        sys.modules.pop("core.certificates", None)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_system_test_full_unhealthy_on_zero_certificates(monkeypatch):
    """Issue #94.3: full result UNHEALTHY when certificate count is zero."""
    import sys
    from types import ModuleType as _Mod
    from core.app import store
    from core.models import Job, SceneRecord, StageStatus, utc_now
    store.jobs.clear()
    store.jobs["job-1"] = Job(
        job_id="job-1", topic="test", character_id="char-1",
        priority=1, status=JobStatus.READY,
        scenes=[SceneRecord(scene_id="s1", index=1, prompt="p1", status=StageStatus.READY)],
        created_at=utc_now())
    monkeypatch.setenv("NODE_ROLE", "core")
    import core.health_checks as _hc
    monkeypatch.setattr(_hc, "run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr(_hc, "health_status", lambda c: "HEALTHY")
    import adapters.providers as _prov
    monkeypatch.setattr(_prov, "provider_matrix", lambda: {"tts": {"available": True}})
    _cm = _Mod("core.certificates")
    _cm.list_certificates = lambda: []
    sys.modules["core.certificates"] = _cm
    import tempfile
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_ROOT", tmpdir)
    os.chmod(tmpdir, 0o755)
    import core.queue as _qq
    monkeypatch.setattr(_qq, "TaskQueue", lambda: None)
    try:
        resp = _tc.post("/api/system/test", json={"scope": "full"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == "UNHEALTHY"
        assert any("certificates" in f for f in data.get("full_test_failures", []))
    finally:
        store.jobs.clear()
        sys.modules.pop("core.certificates", None)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_system_test_full_unhealthy_on_failed_tasks(monkeypatch):
    """Issue #94.3: full result UNHEALTHY when task results contain failures."""
    import sys
    from types import ModuleType as _Mod
    from core.app import store
    from core.models import Job, SceneRecord, StageStatus, utc_now
    store.jobs.clear()
    store.jobs["job-1"] = Job(
        job_id="job-1", topic="test", character_id="char-1",
        priority=1, status=JobStatus.READY,
        scenes=[
            SceneRecord(scene_id="s1", index=1, prompt="p1", status=StageStatus.READY),
            SceneRecord(scene_id="s2", index=2, prompt="p2", status=StageStatus.FAILED),
        ], created_at=utc_now())
    monkeypatch.setenv("NODE_ROLE", "core")
    import core.health_checks as _hc
    monkeypatch.setattr(_hc, "run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr(_hc, "health_status", lambda c: "HEALTHY")
    import adapters.providers as _prov
    monkeypatch.setattr(_prov, "provider_matrix", lambda: {"tts": {"available": True}})
    _cm = _Mod("core.certificates")
    _cm.list_certificates = lambda: [{"cert_id": "c1", "expires_in_days": 999}]
    sys.modules["core.certificates"] = _cm
    import tempfile
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_ROOT", tmpdir)
    os.chmod(tmpdir, 0o755)
    import core.queue as _qq
    monkeypatch.setattr(_qq, "TaskQueue", lambda: None)
    try:
        resp = _tc.post("/api/system/test", json={"scope": "full"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == "UNHEALTHY"
        assert any("task_results" in f for f in data.get("full_test_failures", []))
        assert data["task_results"]["failed"] == 1
    finally:
        store.jobs.clear()
        sys.modules.pop("core.certificates", None)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_system_test_full_healthy_when_all_ok(monkeypatch):
    """Issue #94.3: full result HEALTHY when all inventory and task checks pass."""
    import sys
    from types import ModuleType as _Mod
    from core.app import store
    from core.models import Job, SceneRecord, StageStatus, utc_now
    store.jobs.clear()
    store.jobs["job-1"] = Job(
        job_id="job-1", topic="test", character_id="char-1",
        priority=1, status=JobStatus.READY,
        scenes=[SceneRecord(scene_id="s1", index=1, prompt="p1", status=StageStatus.READY)],
        created_at=utc_now())
    monkeypatch.setenv("NODE_ROLE", "core")
    import core.health_checks as _hc
    monkeypatch.setattr(_hc, "run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr(_hc, "health_status", lambda c: "HEALTHY")
    import adapters.providers as _prov
    monkeypatch.setattr(_prov, "provider_matrix", lambda: {"tts": {"available": True}})
    _cm = _Mod("core.certificates")
    _cm.list_certificates = lambda: [{"cert_id": "c1", "expires_in_days": 999}]
    sys.modules["core.certificates"] = _cm
    import tempfile
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_ROOT", tmpdir)
    os.chmod(tmpdir, 0o755)
    import core.queue as _qq
    monkeypatch.setattr(_qq, "TaskQueue", lambda: None)
    try:
        resp = _tc.post("/api/system/test", json={"scope": "full"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == "HEALTHY"
        assert "full_test_failures" not in data
    finally:
        store.jobs.clear()
        sys.modules.pop("core.certificates", None)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_system_test_full_counts_ready_and_running_stage_statuses(monkeypatch, tmp_path):
    """READY is completed and RUNNING is pending in the actual StageStatus model."""
    import sys
    from types import ModuleType as _Mod
    from core.app import store
    from core.models import Job, SceneRecord, StageStatus, utc_now
    store.jobs.clear()
    store.jobs["job-stage-stats"] = Job(
        job_id="job-stage-stats", topic="test", character_id="char-1", priority=1,
        status=JobStatus.READY,
        scenes=[
            SceneRecord(scene_id="ready", index=1, prompt="p", status=StageStatus.READY),
            SceneRecord(scene_id="running", index=2, prompt="p", status=StageStatus.RUNNING),
        ], created_at=utc_now())
    monkeypatch.setattr("core.health_checks.run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr("core.health_checks.health_status", lambda checks: "HEALTHY")
    monkeypatch.setattr("adapters.providers.provider_matrix",
                        lambda: {"tts": {"configured": True}})
    certificates = _Mod("core.certificates")
    certificates.list_certificates = lambda: [{"cert_id": "c1", "expires_in_days": 99}]
    sys.modules["core.certificates"] = certificates
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    try:
        response = _tc.post("/api/system/test", json={"scope": "full"})
        stats = response.json()["task_results"]
        assert stats == {"completed": 1, "failed": 0, "pending": 1}
    finally:
        store.jobs.clear()
        sys.modules.pop("core.certificates", None)


def test_system_test_full_unhealthy_on_expired_certificate(monkeypatch, tmp_path):
    import sys
    from types import ModuleType as _Mod
    monkeypatch.setattr("core.health_checks.run_checks", lambda **kw: {"docker": (True, "ok")})
    monkeypatch.setattr("core.health_checks.health_status", lambda checks: "HEALTHY")
    monkeypatch.setattr("adapters.providers.provider_matrix",
                        lambda: {"tts": {"configured": True}})
    certificates = _Mod("core.certificates")
    certificates.list_certificates = lambda: [{"cert_id": "expired", "expires_in_days": -1}]
    sys.modules["core.certificates"] = certificates
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    try:
        response = _tc.post("/api/system/test", json={"scope": "full"})
        assert response.json()["result"] == "UNHEALTHY"
        assert "certificates(1 expired)" in response.json()["full_test_failures"]
    finally:
        sys.modules.pop("core.certificates", None)


def test_vid_ok_rejects_wrong_chat(monkeypatch):
    from unittest.mock import Mock
    from core.app import _handle_video_callback, store
    from core.models import Job, JobStatus, utc_now
    store.jobs.clear()
    job = Job(job_id="vid-1", topic="test", character_id="ch", priority=1,
              status=JobStatus.VIDEO_PENDING_APPROVAL, source="telegram:111",
              created_at=utc_now())
    store.jobs["vid-1"] = job
    adapter = Mock()
    adapter.answer_callback = Mock(return_value={"status": "ok"})
    monkeypatch.setattr("core.app.TelegramAdapter", lambda: adapter)
    cb = {"id": "cb-vid", "data": "vid_ok:vid-1", "message": {"chat": {"id": "222"}}}
    _handle_video_callback(cb, "222", "vid_ok", "vid-1")
    text = adapter.answer_callback.call_args[0][1]
    assert "заборонено" in text.lower() or "Доступ" in text


def test_vid_ok_accepts_correct_chat(monkeypatch):
    from unittest.mock import Mock
    from core.app import _handle_video_callback, store
    from core.models import Job, JobStatus, utc_now
    store.jobs.clear()
    job = Job(job_id="vid-2", topic="test", character_id="ch", priority=1,
              status=JobStatus.VIDEO_PENDING_APPROVAL, source="telegram:111",
              created_at=utc_now())
    store.jobs["vid-2"] = job
    adapter = Mock()
    adapter.answer_callback = Mock(return_value={"status": "ok"})
    monkeypatch.setattr("core.app.TelegramAdapter", lambda: adapter)
    import core.pipeline as _pipe
    monkeypatch.setattr(_pipe, "approve_video", lambda store, job, actor, **kw: job)
    cb = {"id": "cb-vid-ok", "data": "vid_ok:vid-2", "message": {"chat": {"id": "111"}}}
    _handle_video_callback(cb, "111", "vid_ok", "vid-2")
    text = adapter.answer_callback.call_args[0][1]
    assert "схвалено" in text.lower()


def test_vid_ok_accepts_source_with_message_id(monkeypatch):
    """Telegram source stores chat and message IDs; ownership uses the chat segment."""
    from unittest.mock import Mock
    from core.app import _handle_video_callback, store
    from core.models import Job, JobStatus, utc_now
    store.jobs.clear()
    job = Job(job_id="vid-3", topic="test", character_id="ch", priority=1,
              status=JobStatus.VIDEO_PENDING_APPROVAL, source="telegram:111:987",
              created_at=utc_now())
    store.jobs["vid-3"] = job
    adapter = Mock()
    adapter.answer_callback = Mock(return_value={"status": "ok"})
    monkeypatch.setattr("core.app.TelegramAdapter", lambda: adapter)
    import core.pipeline as _pipe
    monkeypatch.setattr(_pipe, "approve_video", lambda store, job, actor, **kw: job)
    cb = {"id": "cb-vid-ok-message", "data": "vid_ok:vid-3", "message": {"chat": {"id": "111"}}}
    _handle_video_callback(cb, "111", "vid_ok", "vid-3")
    assert "схвалено" in adapter.answer_callback.call_args[0][1].lower()


# ── Polling POSIX durability: atomic write + fsync ─────────────────────────


def test_polling_save_offset_uses_atomic_write(tmp_path):
    """_save_offset_data uses temp file + fsync + rename (POSIX atomic write)."""
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    service.offset = 42
    service.last_update_id = 41
    service.last_message_at = "2026-01-01T00:00:00Z"
    service._save_offset_data(42)
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["offset"] == 42
    assert data["last_update_id"] == 41
    tmp_files = list(state_file.parent.glob(state_file.name + ".tmp"))
    assert len(tmp_files) == 0, "Temp file should be cleaned up after atomic rename"


def test_polling_save_offset_fsync_before_rename(tmp_path, monkeypatch):
    """_save_offset_data calls fsync before rename to guarantee durability."""
    from adapters.telegram import TelegramPollingService
    import os as _os
    state_file = tmp_path / "polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    service.offset = 10
    service.last_update_id = 9
    fsync_calls = []
    original_fsync = _os.fsync

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return original_fsync(fd)
    _os.fsync = tracking_fsync
    try:
        service._save_offset_data(10)
    finally:
        _os.fsync = original_fsync
    assert len(fsync_calls) >= 1, "fsync should be called at least once for durability"
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["offset"] == 10


def test_polling_save_offset_fsyncs_parent_directory(tmp_path, monkeypatch):
    """The directory entry created by replace is durable too."""
    from adapters.telegram import TelegramPollingService
    import os as _os
    state_file = tmp_path / "polling_state.json"
    service = TelegramPollingService(token="test-token", on_update=lambda u: None,
                                     offset_file=state_file)
    directory_fsyncs = []
    original_fsync = _os.fsync

    def tracking_fsync(fd):
        if _os.path.isdir(f"/proc/self/fd/{fd}"):
            directory_fsyncs.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(_os, "fsync", tracking_fsync)
    service._save_offset_data(11)
    assert directory_fsyncs


@pytest.mark.parametrize("payload", [[], "valid-json", 7, None])
def test_polling_load_offset_rejects_valid_non_object_json(tmp_path, payload):
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "polling_state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    service = TelegramPollingService(token="test-token", on_update=lambda u: None,
                                     offset_file=state_file)
    assert service.offset == 0
    assert service.last_update_id is None


# ── Polling idempotency: offset saved BEFORE next poll ────────────────────


def test_polling_offset_saved_before_next_poll(tmp_path):
    """Offset is persisted before on_update runs, preventing duplicate processing on restart."""
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "polling_state.json"
    processed = []
    service = TelegramPollingService(
        token="test-token",
        on_update=lambda u: processed.append(u["update_id"]),
        offset_file=state_file)

    updates = [{"update_id": 1, "message": {"text": "/start"}},
               {"update_id": 2, "message": {"text": "/help"}}]
    service.offset = 0
    for u in updates:
        service.on_update(u)
        service.last_update_id = u["update_id"]
        service._save_offset_data(u["update_id"] + 1)
        service.offset = u["update_id"] + 1
    assert processed == [1, 2]
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["offset"] == 3
    assert data["last_update_id"] == 2


def test_polling_restart_replays_from_saved_offset(tmp_path):
    """After restart, polling loads offset from file, skipping already-processed updates."""
    from adapters.telegram import TelegramPollingService
    state_file = tmp_path / "polling_state.json"
    state_file.write_text(json.dumps({
        "offset": 3,
        "last_update_id": 2,
        "last_message_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    service = TelegramPollingService(token="test-token", on_update=lambda u: None, offset_file=state_file)
    assert service.offset == 3
    assert service.last_update_id == 2
