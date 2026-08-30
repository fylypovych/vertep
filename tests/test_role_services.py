import base64
import json

from fastapi.testclient import TestClient

from services import (backup_service, certificate_service, dispatcher_service,
                      license_service, publisher_service, scheduler_service, tts_service)


def test_tts_service_returns_valid_wav(monkeypatch):
    class Result:
        stdout = b"RIFF" + b"\0" * 128

    monkeypatch.setattr(tts_service.shutil, "which", lambda _: "/usr/bin/espeak-ng")
    monkeypatch.setattr(tts_service.subprocess, "run", lambda *args, **kwargs: Result())
    response = TestClient(tts_service.app).post(
        "/synthesize", json={"text": "Вітаю", "voice": "uk", "speed": 150})
    assert response.status_code == 200
    assert base64.b64decode(response.json()["audio_base64"]).startswith(b"RIFF")


def test_publisher_service_is_idempotent_and_never_fakes_live_success(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLISHER_RECEIPT_ROOT", str(tmp_path))
    client = TestClient(publisher_service.app)
    payload = {"job_id": "job-1", "payload": {"channel": "youtube"}}
    monkeypatch.delenv("PUBLISHER_MOCK", raising=False)
    assert client.post("/publish", json=payload).status_code == 503
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    first = client.post("/publish", json=payload)
    second = client.post("/publish", json=payload)
    assert first.status_code == 200
    assert first.json() == second.json()
    assert (tmp_path / f"{first.json()['publication_id']}.json").is_file()


def test_backup_service_creates_encrypted_snapshot_and_receipt(monkeypatch, tmp_path):
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "installation.json").write_text('{"id":"installation"}', encoding="utf-8")
    (storage / "artifact.bin").write_bytes(b"content that must not remain plaintext")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode("ascii"))
    client = TestClient(backup_service.app)
    assert client.get("/health").status_code == 200
    response = client.post("/snapshots", json={"job_id": "backup-job", "request": {}})
    assert response.status_code == 200
    receipt = response.json()
    encrypted = backups / receipt["file"]
    assert encrypted.read_bytes().startswith(backup_service.MAGIC)
    assert b"content that must not remain plaintext" not in encrypted.read_bytes()
    assert json.loads((backups / f"{receipt['snapshot_id']}.json").read_text())["sha256"] == receipt["sha256"]
    (config / "installation.json").write_text('{"id":"changed"}', encoding="utf-8")
    (storage / "artifact.bin").write_bytes(b"changed")
    restored = client.post(f"/snapshots/{receipt['snapshot_id']}/restore")
    assert restored.status_code == 200
    assert (config / "installation.json").read_text(encoding="utf-8") == '{"id":"installation"}'
    assert (storage / "artifact.bin").read_bytes() == b"content that must not remain plaintext"


def test_isolated_runtime_services_are_healthy(monkeypatch, tmp_path):
    assert TestClient(dispatcher_service.app).get("/health").json()["service"] == "dispatcher"
    assert TestClient(scheduler_service.app).get("/health").json()["service"] == "scheduler"
    monkeypatch.delenv("VERTEP_LICENSE_KEY", raising=False)
    monkeypatch.delenv("VERTEP_LICENSE_KEY_FILE", raising=False)
    license_status = TestClient(license_service.app).get("/status").json()
    assert license_status == {"status": "HEALTHY", "state": "COMMUNITY", "fingerprint": ""}


def test_certificate_manager_renews_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("TLS_ROOT", str(tmp_path))
    monkeypatch.setenv("WEB_DOMAIN", "vertep.example")
    client = TestClient(certificate_service.app)
    renewed = client.post("/certificate/renew")
    assert renewed.status_code == 200
    assert renewed.json()["status"] == "HEALTHY"
    assert (tmp_path / "vertep.crt").read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert (tmp_path / "vertep.key").read_text().startswith("-----BEGIN PRIVATE KEY-----")


def test_scheduler_filters_future_jobs():
    response = TestClient(scheduler_service.app).post("/due", json={"jobs": [
        {"job_id": "future", "scheduled_for": "2999-01-01T00:00:00Z", "priority": 99},
        {"job_id": "ready", "scheduled_for": None, "priority": 1},
    ], "limit": 10})
    assert [item["job_id"] for item in response.json()["jobs"]] == ["ready"]
