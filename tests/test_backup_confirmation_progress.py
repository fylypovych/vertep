"""Тести для core/backup/ — confirmation/progress/guard (Issue #186.x)."""

import os
import tempfile
from pathlib import Path

from core.backup.resume import ResumeManager, RestoreState
from core.backup.snapshot import Snapshot
from core.backup.storage import SnapshotStorage


def _make_storage(tmp_path):
    return SnapshotStorage(root=str(tmp_path / "backups"))


def test_confirmation_single_use_bound_to_snapshot(tmp_path):
    storage = _make_storage(tmp_path)
    state = RestoreState(storage)
    snapshot = Snapshot.create(jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0")
    sid = storage.save_snapshot(snapshot)

    import secrets as _secrets
    token = _secrets.token_hex(16)
    state.write_confirmation(sid, token)

    assert state.consume_confirmation(sid, token) is True
    # Same token again must be refused (single use).
    assert state.consume_confirmation(sid, token) is False
    # Wrong token must be refused.
    assert state.consume_confirmation(sid, "wrong-token") is False


def test_confirmation_survives_restart(tmp_path):
    storage = _make_storage(tmp_path)
    snapshot = Snapshot.create(jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0")
    sid = storage.save_snapshot(snapshot)

    state1 = RestoreState(storage)
    state1.write_confirmation(sid, "token-abc")

    # New state object simulates a service restart reading durable files.
    state2 = RestoreState(storage)
    assert state2.consume_confirmation(sid, "token-abc") is True
    assert state2.consume_confirmation(sid, "token-abc") is False


def test_progress_write_and_read(tmp_path):
    storage = _make_storage(tmp_path)
    state = RestoreState(storage)
    sid = "snap-test-1"

    state.write_operation(sid, {"status": "running", "progress": 50, "message": "mid-flight"})
    op = state.load_operation(sid)
    assert op is not None
    assert op["status"] == "running"
    assert op["progress"] == 50
    assert op["message"] == "mid-flight"


def test_progress_survives_restart(tmp_path):
    storage = _make_storage(tmp_path)
    sid = "snap-test-2"

    state1 = RestoreState(storage)
    state1.write_operation(sid, {"status": "done", "progress": 100})

    state2 = RestoreState(storage)
    op = state2.load_operation(sid)
    assert op is not None
    assert op["status"] == "done"
    assert op["progress"] == 100


def test_guard_excludes_competing_restore(tmp_path):
    storage = _make_storage(tmp_path)
    state = RestoreState(storage)
    sid = "snap-test-3"

    guard1 = state.acquire_guard(sid)
    assert guard1 is not None

    # Competing restore cannot acquire the guard while TTL holds.
    guard2 = state.acquire_guard(sid)
    assert guard2 is None

    state.release_guard(guard1)

    # After release, another restore can acquire it.
    guard3 = state.acquire_guard(sid)
    assert guard3 is not None
    state.release_guard(guard3)


def test_guard_owner_only_can_release(tmp_path):
    storage = _make_storage(tmp_path)
    state = RestoreState(storage)
    sid = "snap-test-4"

    guard1 = state.acquire_guard(sid)
    assert guard1 is not None

    # A different state object (simulating another process) cannot release it.
    state2 = RestoreState(storage)
    state2.release_guard(guard1)

    # Guard must still be held by the first process.
    guard2 = state.acquire_guard(sid)
    assert guard2 is None

    # The owner can release it.
    state.release_guard(guard1)
    guard3 = state.acquire_guard(sid)
    assert guard3 is not None
    state.release_guard(guard3)


def test_guard_expires_after_ttl(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    state = RestoreState(storage)
    sid = "snap-test-5"

    guard1 = state.acquire_guard(sid)
    assert guard1 is not None

    # Expire the guard by rewriting its started_at to the past.
    import json
    guard_path = state.state_dir / "restore.guard"
    data = json.loads(guard_path.read_text(encoding="utf-8"))
    data["started_at"] = 0  # far in the past
    guard_path.write_text(json.dumps(data), encoding="utf-8")

    # Another state object can now steal the expired guard.
    state2 = RestoreState(storage)
    guard2 = state2.acquire_guard(sid)
    assert guard2 is not None
    state2.release_guard(guard2)
