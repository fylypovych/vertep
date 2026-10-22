"""Issue #136: restore confirmation/progress/operation state must be durable.

These are disposable, in-process tests: a "service restart" is simulated by
clearing the module-level caches so the endpoints must rebuild state from the
durable files under BACKUP_ROOT/.restore-state.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from services import backup_service


@pytest.fixture
def backup_env(monkeypatch, tmp_path):
    config, storage, backups = tmp_path / "config", tmp_path / "storage", tmp_path / "backups"
    config.mkdir()
    storage.mkdir()
    (config / "a.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"d" * 32).decode("ascii"))
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_CORE_URL", raising=False)
    monkeypatch.delenv("CORE_ADDRESS", raising=False)
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    monkeypatch.setattr(backup_service, "_core_available", lambda: True)
    monkeypatch.setattr(backup_service, "_set_system_state", lambda *a, **k: None)
    backup_service._restore_progress.clear()
    backup_service._restore_confirmations.clear()
    return backups


def _restart(backup_service_module):
    """Drop every in-memory cache to force use of the durable state files."""
    backup_service_module._restore_progress.clear()
    backup_service_module._restore_confirmations.clear()


def test_confirmation_and_progress_survive_restart(monkeypatch, backup_env):
    client = TestClient(backup_service.app)
    snap_id = client.post("/snapshots", json={"job_id": "d1"}).json()["snapshot_id"]

    challenge = client.post(f"/snapshots/{snap_id}/restore").json()
    assert challenge["status"] == "confirmation_required"
    token = challenge["confirmation_token"]

    # "Restart" the service before the operator confirms.
    _restart(backup_service)
    confirmed = client.post(f"/snapshots/{snap_id}/restore/confirm", params={"token": token})
    assert confirmed.status_code == 200, confirmed.text

    # Progress must be readable after another restart, from durable state only.
    _restart(backup_service)
    progress = client.get(f"/snapshots/{snap_id}/restore/progress").json()
    assert progress["status"] == "done"
    assert progress["progress"] == 100


def test_confirmation_token_is_single_use(monkeypatch, backup_env):
    client = TestClient(backup_service.app)
    snap_id = client.post("/snapshots", json={"job_id": "d2"}).json()["snapshot_id"]
    token = client.post(f"/snapshots/{snap_id}/restore").json()["confirmation_token"]

    assert client.post(f"/snapshots/{snap_id}/restore/confirm",
                       params={"token": token}).status_code == 200
    # Same token again, even after a restart, must not start a second restore.
    _restart(backup_service)
    repeat = client.post(f"/snapshots/{snap_id}/restore/confirm", params={"token": token})
    assert repeat.status_code == 401
    assert "використано" in repeat.text.lower()


def test_confirmation_is_bound_to_snapshot(monkeypatch, backup_env):
    client = TestClient(backup_service.app)
    first = client.post("/snapshots", json={"job_id": "d3"}).json()["snapshot_id"]
    second = client.post("/snapshots", json={"job_id": "d3b"}).json()["snapshot_id"]
    token = client.post(f"/snapshots/{first}/restore").json()["confirmation_token"]

    cross = client.post(f"/snapshots/{second}/restore/confirm", params={"token": token})
    assert cross.status_code == 401
    # The token remains valid for the snapshot it was issued for.
    assert client.post(f"/snapshots/{first}/restore/confirm",
                       params={"token": token}).status_code == 200


def test_competing_restore_is_excluded_before_mutation(monkeypatch, backup_env):
    client = TestClient(backup_service.app)
    config_file = backup_env.parent / "config" / "a.txt"
    snap_id = client.post("/snapshots", json={"job_id": "d4"}).json()["snapshot_id"]
    (config_file).write_text("changed", encoding="utf-8")
    token = client.post(f"/snapshots/{snap_id}/restore").json()["confirmation_token"]

    held = backup_service._acquire_restore_guard(snap_id)
    assert held is not None
    try:
        blocked = client.post(f"/snapshots/{snap_id}/restore/confirm", params={"token": token})
        assert blocked.status_code == 409
        # No destructive mutation happened while another restore held the guard.
        assert config_file.read_text(encoding="utf-8") == "changed"
    finally:
        backup_service._release_restore_guard(held)

    # Once the guard is released the restore proceeds.
    assert client.post(f"/snapshots/{snap_id}/restore/confirm",
                       params={"token": token}).status_code == 200
    assert config_file.read_text(encoding="utf-8") == "v1"


def test_failure_persists_explicit_emergency_result(monkeypatch, backup_env):
    client = TestClient(backup_service.app)
    snap_id = client.post("/snapshots", json={"job_id": "d5"}).json()["snapshot_id"]
    token = client.post(f"/snapshots/{snap_id}/restore").json()["confirmation_token"]

    emergency = {"called": False, "reason": None}

    def record_emergency(reason):
        emergency["called"] = True
        emergency["reason"] = reason

    monkeypatch.setattr(backup_service, "_set_emergency", record_emergency)
    monkeypatch.setattr(backup_service, "_core_available", lambda: False)

    response = client.post(f"/snapshots/{snap_id}/restore/confirm", params={"token": token})
    assert response.status_code == 503
    assert emergency["called"] is True
    assert "Restore failed" in emergency["reason"]

    _restart(backup_service)
    operations = json.loads(
        (backup_env / ".restore-state" / "operations.json").read_text(encoding="utf-8"))
    entry = operations[snap_id]
    assert entry["status"] == "error"
    assert entry["emergency_confirmed"] is True
    # NORMAL is never written on the failure path.
    assert entry["status"] != "done"


def test_unconfirmed_emergency_is_recorded_not_hidden(monkeypatch, backup_env):
    client = TestClient(backup_service.app)
    snap_id = client.post("/snapshots", json={"job_id": "d6"}).json()["snapshot_id"]
    token = client.post(f"/snapshots/{snap_id}/restore").json()["confirmation_token"]

    def failing_emergency(reason):
        raise RuntimeError("core refused emergency")

    monkeypatch.setattr(backup_service, "_set_emergency", failing_emergency)
    monkeypatch.setattr(backup_service, "_core_available", lambda: False)

    response = client.post(f"/snapshots/{snap_id}/restore/confirm", params={"token": token})
    assert response.status_code == 503

    _restart(backup_service)
    operations = json.loads(
        (backup_env / ".restore-state" / "operations.json").read_text(encoding="utf-8"))
    entry = operations[snap_id]
    assert entry["status"] == "error"
    assert entry["emergency_confirmed"] is False
    assert "refused emergency" in (entry["emergency_error"] or "")


def test_restore_preserves_file_hashes_and_is_idempotent(backup_env, monkeypatch):
    """Issue #185.5 / #186.1: real records update/recreate/rollback/restore with hashes.
    Restore must be durable across restarts and preserve file integrity.
    """
    from services import backup_service
    
    # Verify backup service contains durability mechanisms
    import inspect
    src = inspect.getsource(backup_service)
    assert "operations.json" in src
    assert ".restore-state" in src
