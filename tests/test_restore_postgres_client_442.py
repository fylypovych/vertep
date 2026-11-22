"""#442 restore: pg_restore 17 against PostgreSQL 16 failed after files were replaced, the
test brand and workflow vanished and the system went EMERGENCY while /api/health said HEALTHY."""

import base64
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from services import backup_service

ROOT = Path(__file__).resolve().parents[1]


def _restore(client, snapshot_id):
    token = client.post(f"/snapshots/{snapshot_id}/restore").json()["confirmation_token"]
    return client.post(f"/snapshots/{snapshot_id}/restore/confirm", params={"token": token})


def test_the_backup_image_ships_the_postgres_client_of_the_server_major():
    server = re.search(r"postgres:(\d+)", (ROOT / "docker/postgres/Dockerfile").read_text()).group(1)
    backup = (ROOT / "docker/backup/Dockerfile").read_text()
    assert f"ARG POSTGRES_MAJOR={server}" in backup and 'postgresql-client-${POSTGRES_MAJOR}' in backup
    assert re.search(r"\bpostgresql-client\b(?!-)", backup) is None  # never Debian's default client
    compose = (ROOT / "docker-compose.yml").read_text()
    assert f"postgres:{server}-alpine" in compose


def test_a_mismatched_client_refuses_the_restore_before_anything_changes(monkeypatch, tmp_path):
    config, storage = tmp_path / "config", tmp_path / "storage"
    config.mkdir()
    storage.mkdir()
    (config / "a.txt").write_text("snapshot", encoding="utf-8")
    for name, value in {"BACKUP_CONFIG_ROOT": config, "BACKUP_STORAGE_ROOT": storage,
                        "BACKUP_ROOT": tmp_path / "backups"}.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"f" * 32).decode("ascii"))
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_PG_RESTORE_CMD", raising=False)
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    monkeypatch.setattr(backup_service, "_core_available", lambda: True)
    states, emergencies = [], []
    monkeypatch.setattr(backup_service, "_set_system_state", lambda state, reason, **_: states.append(state))
    monkeypatch.setattr(backup_service, "_set_emergency", emergencies.append)
    monkeypatch.setattr(backup_service, "_command_exists", lambda command: True)

    def run(command, *, timeout, env=None):
        output = b"pg_restore (PostgreSQL) 17.2\n" if command[:2] == ["pg_restore", "--version"] else b"160004\n"
        return subprocess.CompletedProcess(command, 0, output, b"")
    monkeypatch.setattr(backup_service, "_run_command", run)
    client = TestClient(backup_service.app)
    snapshot = client.post("/snapshots", json={"job_id": "job-1", "request": {}}).json()["snapshot_id"]
    (config / "a.txt").write_text("live data", encoding="utf-8")

    response = _restore(client, snapshot)
    assert response.status_code == 409 and "pg_restore 17" in response.json()["detail"]
    assert (config / "a.txt").read_text(encoding="utf-8") == "live data"  # nothing replaced
    assert emergencies == [] and states[-1] == "NORMAL"


def test_the_versions_match_or_cannot_be_read_and_the_restore_goes_on(monkeypatch):
    monkeypatch.delenv("BACKUP_PG_RESTORE_CMD", raising=False)
    monkeypatch.setattr(backup_service, "_command_exists", lambda command: True)
    monkeypatch.setattr(backup_service, "_run_command", lambda command, **_: subprocess.CompletedProcess(
        command, 0, b"pg_restore (PostgreSQL) 16.6\n" if "--version" in command else b"160004\n", b""))
    assert backup_service._pg_client_mismatch() is None
    monkeypatch.setattr(backup_service, "_command_exists", lambda command: False)
    assert backup_service._pg_client_mismatch() is None


def test_health_reports_the_global_state_next_to_the_component_checks(monkeypatch):
    from core.api import observability
    monkeypatch.setattr(observability, "_run_health_checks", lambda _: {})
    monkeypatch.setattr("core.system_state.get_system_state", lambda: {"state": "EMERGENCY"})
    assert observability.health()["system_state"] == "EMERGENCY"
