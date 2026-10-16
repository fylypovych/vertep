import errno
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from services import backup_service


def test_production_backup_storage_contract():
    compose = yaml.safe_load(Path("deploy/docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["backup-service"]
    assert service["environment"]["BACKUP_ROOT"] == "/data/backups"
    assert "./backups:/data/backups" in service["volumes"]
    assert service["read_only"] is True
    assert "/tmp:rw,noexec,nosuid,size=128m" in service["tmpfs"]
    assert service["environment"]["BACKUP_ENCRYPTION_KEY_FILE"] == "/run/secrets/backup_encryption_key"
    assert "backup_encryption_key" in service["secrets"]
    for override in ("amd", "nvidia"):
        data = yaml.safe_load(Path(f"deploy/docker-compose.{override}.yml").read_text(encoding="utf-8"))
        assert "backup-service" not in data["services"]


@pytest.fixture
def backup_storage(monkeypatch, tmp_path):
    root = tmp_path / "mounted-backups"
    key_file = tmp_path / "backup_encryption_key"
    key_file.write_text("ab" * 32, encoding="ascii")
    monkeypatch.setenv("BACKUP_ROOT", str(root))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    # Any fallback into the image filesystem would fail, even on Windows/root.
    project = tmp_path / "app"
    project.write_text("read-only image placeholder", encoding="utf-8")
    monkeypatch.setattr(backup_service, "_PROJECT_ROOT", project)
    return root


def test_health_and_snapshot_use_mounted_storage(monkeypatch, tmp_path, backup_storage):
    source = tmp_path / "config"
    source.mkdir()
    (source / "test.txt").write_text("persistent data", encoding="utf-8")
    monkeypatch.setenv("BACKUP_SOURCES", f"config:{source}")
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_REMOTE_CMD", raising=False)
    with TestClient(backup_service.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "HEALTHY", "encryption": "AES-256-GCM"}
        assert list(backup_storage.iterdir()) == []
        response = client.post("/snapshots", json={"job_id": "persistent"})
        assert response.status_code == 200
        receipt = response.json()
    assert (backup_storage / receipt["file"]).read_bytes().startswith(backup_service.MAGIC)
    assert (backup_storage / f"{receipt['snapshot_id']}.json").is_file()
    # A fresh API client sees files persisted in the configured directory.
    with TestClient(backup_service.app) as client:
        assert client.get("/snapshots").json()["snapshots"] == [receipt]


def test_sealed_secret_store_survives_snapshot_and_restore(monkeypatch, tmp_path, backup_storage):
    """Issue #84: a snapshot must carry the sealed data key and the encrypted
    store together, so a restore on disposable storage yields a store that is
    still openable with the same passphrase and not with a wrong one."""
    import core.first_run as first_run
    from services import backup_service

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BACKUP_SOURCES", f"config:{config}")
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_REMOTE_CMD", raising=False)

    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-very-strong-passphrase")
    first_run.set_integration_secret("telegram_bot_token", "backup-me")
    assert (config / "secret-store.key").is_file()
    assert (config / "secrets.enc.json").is_file()

    with TestClient(backup_service.app) as client:
        receipt = client.post("/snapshots", json={"job_id": "persistent"}).json()

    archive_bytes = (backup_storage / receipt["file"]).read_bytes()
    assert b"backup-me" not in archive_bytes
    assert b"a-very-strong-passphrase" not in archive_bytes

    # Restore on disposable storage (an empty destination root).
    restored = tmp_path / "restored-config"
    restored.mkdir()
    monkeypatch.setattr(backup_service, "_core_available", lambda: True)
    monkeypatch.setattr(backup_service, "_set_system_state", lambda *a, **k: None)
    monkeypatch.setattr(backup_service, "_set_restore_progress", lambda *a, **k: None)
    monkeypatch.setenv("BACKUP_SOURCES", f"config:{restored}")
    with TestClient(backup_service.app) as client:
        challenge = client.post(f"/snapshots/{receipt['snapshot_id']}/restore").json()
        assert challenge["status"] == "confirmation_required"
        response = client.post(f"/snapshots/{receipt['snapshot_id']}/restore/confirm",
                               params={"token": challenge["confirmation_token"]})
        assert response.status_code == 200, response.text

    assert (restored / "secret-store.key").is_file()
    assert (restored / "secrets.enc.json").is_file()
    monkeypatch.setenv("CONFIG_ROOT", str(restored))
    assert first_run.ensure_secret_store()["telegram_bot_token"] == "backup-me"

    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "another-strong-passphrase")
    with pytest.raises(ValueError, match="authentication"):
        first_run.ensure_secret_store()


@pytest.mark.parametrize("failure", ["mkdir", "open", "write"])
@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.ENOSPC])
def test_health_returns_503_for_unwritable_storage(monkeypatch, backup_storage, failure, code):
    def fail(*args, **kwargs):
        raise OSError(code, "storage unavailable")

    if failure == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    elif failure == "open":
        monkeypatch.setattr(backup_service.tempfile, "TemporaryFile", fail)
    else:
        class FailedWrite:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            write = fail

        monkeypatch.setattr(backup_service.tempfile, "TemporaryFile", lambda **kwargs: FailedWrite())
    response = TestClient(backup_service.app).get("/health")
    assert response.status_code == 503
    assert "недоступний для запису" in response.json()["detail"]


def test_core_proxy_smoke_with_real_backup_service(monkeypatch, tmp_path, backup_storage):
    """Issue #160: Web UI→CORE /api/system/backups→separate Backup service→
    snapshot→list/progress/result over real HTTP, without mock orchestration.

    The CORE production route proxies to an actually running Backup service
    instance on a local socket: no patched httpx transport in the chain.
    """
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from core.app import app as core_app

    source = tmp_path / "config"
    source.mkdir()
    (source / "persistent.txt").write_text("persistent data", encoding="utf-8")
    monkeypatch.setenv("BACKUP_SOURCES", f"config:{source}")
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_REMOTE_CMD", raising=False)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(
        backup_service.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            try:
                healthy = httpx.get(f"{base}/health", timeout=1).status_code == 200
                if healthy:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert healthy, "separate Backup service did not become healthy"

        monkeypatch.setenv("BACKUP_URL", base)
        core = TestClient(core_app)
        created = core.post("/api/system/backups", json={"job_id": "smoke-160"})
        assert created.status_code == 200, created.text
        receipt = created.json()
        assert receipt["snapshot_id"]
        assert len(receipt["sha256"]) == 64
        assert receipt["size_bytes"] > 0
        # The encrypted artifact really exists in the service storage.
        artifact = backup_storage / receipt["file"]
        assert artifact.is_file()
        assert artifact.read_bytes().startswith(backup_service.MAGIC)

        listed = core.get("/api/system/backups")
        assert listed.status_code == 200, listed.text
        assert [s["snapshot_id"] for s in listed.json()["snapshots"]] == [receipt["snapshot_id"]]

        # Progress is a real read of the running service; no restore is started here
        # (restore evidence lives in #136/#137).
        progress = core.get(f"/api/system/backups/{receipt['snapshot_id']}/restore/progress")
        assert progress.status_code == 200, progress.text
        body = progress.json()
        assert body["status"] == "unknown"
        assert body["progress"] == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
