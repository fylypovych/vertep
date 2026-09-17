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
