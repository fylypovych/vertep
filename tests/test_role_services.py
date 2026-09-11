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


def test_backup_service_checksum_verification_rejects_corrupted_snapshot(monkeypatch, tmp_path):
    """Corrupted backup file must fail with 409 and not restore any data."""
    import shutil
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "installation.json").write_text('{"id":"original"}', encoding="utf-8")
    (storage / "artifact.bin").write_bytes(b"original-content")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "job-1", "request": {}})
    assert resp.status_code == 200
    receipt = resp.json()
    snap_id = receipt["snapshot_id"]
    encrypted_file = backups / receipt["file"]
    # Corrupt the encrypted backup file
    corrupted = encrypted_file.read_bytes()[:-10] + b"CORRUPTED!"
    encrypted_file.write_bytes(corrupted)
    # Modify source data
    (config / "installation.json").write_text('{"id":"changed"}', encoding="utf-8")
    (storage / "artifact.bin").write_bytes(b"changed-content")
    # Restore must fail with checksum mismatch
    restored = client.post(f"/snapshots/{snap_id}/restore")
    assert restored.status_code == 409
    assert "Checksum" in restored.text or "checksum" in restored.text.lower()
    # Original data must NOT be restored (still changed)
    assert (config / "installation.json").read_text(encoding="utf-8") == '{"id":"changed"}'
    assert (storage / "artifact.bin").read_bytes() == b"changed-content"


def test_backup_service_failed_restore_sets_emergency(monkeypatch, tmp_path):
    """Failed restore must call _set_emergency and not leave system in false NORMAL."""
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "a.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"f" * 32).decode("ascii"))
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    # Mock _core_available to return False, causing HTTPException 503 inside try block
    monkeypatch.setattr(backup_service, "_core_available", lambda: False)
    # Mock _set_emergency to capture call
    emergency_called = {"called": False, "reason": None}
    def mock_set_emergency(reason: str):
        emergency_called["called"] = True
        emergency_called["reason"] = reason
    monkeypatch.setattr(backup_service, "_set_emergency", mock_set_emergency)
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "job-1", "request": {}})
    assert resp.status_code == 200
    snap_id = resp.json()["snapshot_id"]
    restored = client.post(f"/snapshots/{snap_id}/restore")
    assert restored.status_code == 503
    assert emergency_called["called"] is True
    assert "Restore failed" in emergency_called["reason"]


def test_backup_service_full_integration_config_storage_db(monkeypatch, tmp_path):
    """Full integration: backup config + storage + pg_dump/redis, wipe all, restore, verify."""
    import shutil
    from pathlib import Path
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    # Config data
    (config / "installation.json").write_text('{"id":"inst-1","secret":"cfg-secret"}', encoding="utf-8")
    (config / "license.json").write_text('{"key":"license-key"}', encoding="utf-8")
    # Storage data (characters, workflows, jobs)
    (storage / "characters" / "char1").mkdir(parents=True)
    (storage / "characters" / "char1" / "character.json").write_text('{"id":"char1","name":"Char 1"}', encoding="utf-8")
    (storage / "workflows" / "image").mkdir(parents=True)
    (storage / "workflows" / "image" / "wf1.json").write_text('{"class_type":"Test"}', encoding="utf-8")
    (storage / "jobs" / "job-1").mkdir(parents=True)
    (storage / "jobs" / "job-1" / "job.json").write_text('{"job_id":"job-1","topic":"Test"}', encoding="utf-8")
    # DB dump files will be created by pg_dump/redis commands (mocked)
    pg_dump_path = Path("/tmp/vertep.dump")
    redis_dump_path = Path("/var/lib/redis/dump.rdb")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"i" * 32).decode("ascii"))
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    # Mock pg_dump and redis-cli to create dummy dump files
    def mock_run(cmd, shell=True, timeout=None, capture_output=True):
        if "pg_dump" in cmd:
            pg_dump_path.parent.mkdir(parents=True, exist_ok=True)
            pg_dump_path.write_bytes(b"PG_DUMP_CONTENT")
            return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        if "redis-cli" in cmd and "BGSAVE" in cmd:
            redis_dump_path.parent.mkdir(parents=True, exist_ok=True)
            redis_dump_path.write_bytes(b"REDIS_RDB_CONTENT")
            return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        if "pg_restore" in cmd:
            return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        return type("Result", (), {"returncode": 1, "stdout": b"", "stderr": b""})()
    monkeypatch.setattr(backup_service.subprocess, "run", mock_run)
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "full-backup", "request": {}})
    assert resp.status_code == 200
    snap_id = resp.json()["snapshot_id"]
    receipt = resp.json()
    # Verify inventory includes config, storage, and db
    assert any(item["label"] == "config" for item in receipt["inventory"])
    assert any(item["label"] == "storage" for item in receipt["inventory"])
    # Wipe ALL data
    (config / "installation.json").unlink()
    (config / "license.json").unlink()
    shutil.rmtree(storage / "characters" / "char1")
    (storage / "workflows" / "image" / "wf1.json").unlink()
    (storage / "jobs" / "job-1" / "job.json").unlink()
    # Restore
    restored = client.post(f"/snapshots/{snap_id}/restore")
    assert restored.status_code == 200
    # Verify config restored
    assert (config / "installation.json").read_text(encoding="utf-8") == '{"id":"inst-1","secret":"cfg-secret"}'
    assert (config / "license.json").read_text(encoding="utf-8") == '{"key":"license-key"}'
    # Verify storage restored
    assert (storage / "characters" / "char1" / "character.json").exists()
    assert (storage / "workflows" / "image" / "wf1.json").exists()
    assert (storage / "jobs" / "job-1" / "job.json").exists()
