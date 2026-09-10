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


def test_backup_service_accepts_bootstrap_hex_key(monkeypatch, tmp_path):
    key_file = tmp_path / "backup.key"
    key_file.write_text("6b" * 32, encoding="ascii")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "backups"))

    response = TestClient(backup_service.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "HEALTHY", "encryption": "AES-256-GCM"}
    assert backup_service._key() == b"k" * 32


def test_isolated_runtime_services_are_healthy(monkeypatch, tmp_path):
    assert TestClient(dispatcher_service.app).get("/health").json()["service"] == "dispatcher"
    assert TestClient(scheduler_service.app).get("/health").json()["service"] == "scheduler"
    monkeypatch.delenv("VERTEP_LICENSE_KEY", raising=False)
    monkeypatch.delenv("VERTEP_LICENSE_KEY_FILE", raising=False)
    license_status = TestClient(license_service.app).get("/status").json()
    assert license_status == {"status": "HEALTHY", "state": "COMMUNITY", "fingerprint": ""}


def test_license_health_does_not_initialize_an_empty_read_only_store(monkeypatch, tmp_path):
    monkeypatch.delenv("VERTEP_LICENSE_KEY", raising=False)
    monkeypatch.delenv("VERTEP_LICENSE_KEY_FILE", raising=False)
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "core.first_run.ensure_secret_store",
        lambda: (_ for _ in ()).throw(AssertionError("healthcheck must not initialize storage")),
    )

    response = TestClient(license_service.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "HEALTHY", "state": "COMMUNITY", "fingerprint": ""}


def test_certificate_manager_renews_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("TLS_ROOT", str(tmp_path))
    monkeypatch.setenv("WEB_DOMAIN", "vertep.example")
    client = TestClient(certificate_service.app)
    renewed = client.post("/certificate/renew")
    assert renewed.status_code == 200
    assert renewed.json()["status"] == "HEALTHY"
    assert (tmp_path / "vertep.crt").read_text().startswith("-----BEGIN CERTIFICATE-----")
    private_key_header = "-----BEGIN " + "PRIVATE KEY-----"
    assert (tmp_path / "vertep.key").read_text().startswith(private_key_header)


def test_scheduler_filters_future_jobs():
    response = TestClient(scheduler_service.app).post("/due", json={"jobs": [
        {"job_id": "future", "scheduled_for": "2999-01-01T00:00:00Z", "priority": 99},
        {"job_id": "ready", "scheduled_for": None, "priority": 1},
    ], "limit": 10})
    assert [item["job_id"] for item in response.json()["jobs"]] == ["ready"]


def test_backup_service_retention_removes_old_snapshots(monkeypatch, tmp_path):
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "a.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"r" * 32).decode("ascii"))
    monkeypatch.setenv("BACKUP_MAX_SNAPSHOTS", "2")
    client = TestClient(backup_service.app)
    for i in range(3):
        (config / "a.txt").write_text(f"v{i}", encoding="utf-8")
        resp = client.post("/snapshots", json={"job_id": f"job-{i}", "request": {}})
        assert resp.status_code == 200
    data = client.get("/snapshots").json()
    assert len(data["snapshots"]) == 2


def test_backup_service_blocks_restore_in_emergency(monkeypatch, tmp_path):
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "a.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"e" * 32).decode("ascii"))
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: {"state": "EMERGENCY"})
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "job-1", "request": {}})
    assert resp.status_code == 200
    snap_id = resp.json()["snapshot_id"]
    restored = client.post(f"/snapshots/{snap_id}/restore")
    assert restored.status_code == 409
    assert "EMERGENCY" in restored.text


def test_backup_service_restore_progress_endpoint(monkeypatch, tmp_path):
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "a.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"p" * 32).decode("ascii"))
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "job-1", "request": {}})
    snap_id = resp.json()["snapshot_id"]
    # Before restore — unknown progress
    prog = client.get(f"/snapshots/{snap_id}/restore/progress")
    assert prog.status_code == 200
    assert prog.json()["status"] == "unknown"
    # After restore — done
    client.post(f"/snapshots/{snap_id}/restore")
    prog2 = client.get(f"/snapshots/{snap_id}/restore/progress")
    assert prog2.status_code == 200
    assert prog2.json()["status"] == "done"
    assert prog2.json()["progress"] == 100


def test_backup_service_custom_sources(monkeypatch, tmp_path):
    config, storage, backups, custom = tmp_path / "config", tmp_path / "storage", tmp_path / "backups", tmp_path / "custom"
    config.mkdir()
    storage.mkdir()
    custom.mkdir()
    (custom / "secret.txt").write_text("custom-data", encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_SOURCES", f"custom:{custom}")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"c" * 32).decode("ascii"))
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "job-1", "request": {}})
    assert resp.status_code == 200
    assert any(item["label"] == "custom" for item in resp.json()["inventory"])
    # Wipe and restore
    (custom / "secret.txt").write_text("changed", encoding="utf-8")
    snap_id = resp.json()["snapshot_id"]
    client.post(f"/snapshots/{snap_id}/restore")
    assert (custom / "secret.txt").read_text(encoding="utf-8") == "custom-data"
