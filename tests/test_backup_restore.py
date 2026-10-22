"""Тести для core/backup/ — snapshot, storage, resume."""

import os
import tempfile
from pathlib import Path

from core.backup.snapshot import Snapshot
from core.backup.storage import SnapshotStorage
from core.backup.resume import ResumeManager


def test_snapshot_create_and_compute_checksum():
    snapshot = Snapshot.create(
        jobs=[{"job_id": "j1", "status": "READY"}],
        tasks=[{"task_id": "t1", "job_id": "j1"}],
        artifacts=[{"filename": "a.txt", "kind": "text"}],
        settings={"key": "value"},
        version="0.0.2.74",
    )
    assert snapshot.snapshot_id.startswith("snap-")
    assert snapshot.checksum != ""
    assert snapshot.version == "0.0.2.74"


def test_snapshot_verify_passes():
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0",
    )
    assert snapshot.verify() is True


def test_snapshot_verify_fails_after_tamper():
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0",
    )
    snapshot.jobs.append({"job_id": "tampered"})
    assert snapshot.verify() is False


def test_snapshot_to_dict_from_dict_roundtrip():
    snapshot = Snapshot.create(
        jobs=[{"job_id": "j1"}], tasks=[], artifacts=[], settings={}, version="1.0.0.0",
    )
    data = snapshot.to_dict()
    restored = Snapshot.from_dict(data)
    assert restored.snapshot_id == snapshot.snapshot_id
    assert restored.checksum == snapshot.checksum
    assert restored.jobs == snapshot.jobs


def test_storage_save_and_load(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0",
    )
    sid = storage.save_snapshot(snapshot)
    assert sid == snapshot.snapshot_id
    loaded = storage.load_snapshot(sid)
    assert loaded.snapshot_id == sid
    assert loaded.verify() is True


def test_storage_list_snapshots(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    s1 = Snapshot.create(jobs=[], tasks=[], artifacts=[], settings={}, version="1.0.0.0")
    s2 = Snapshot.create(jobs=[], tasks=[], artifacts=[], settings={}, version="2.0.0.0", incremental=True, parent_snapshot_id=s1.snapshot_id)
    storage.save_snapshot(s1)
    storage.save_snapshot(s2)
    listed = storage.list_snapshots()
    assert len(listed) == 2
    ids = {item["snapshot_id"] for item in listed}
    assert s1.snapshot_id in ids
    assert s2.snapshot_id in ids


def test_storage_delete_snapshot(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    snapshot = Snapshot.create(jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0")
    sid = storage.save_snapshot(snapshot)
    assert storage.delete_snapshot(sid) is True
    assert storage.delete_snapshot(sid) is False


def test_resume_manager_restore_jobs(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[{"job_id": "j1", "status": "READY"}, {"job_id": "j2", "status": "FAILED"}],
        tasks=[], artifacts=[], settings={}, version="0.0.0.0",
    )
    sid = storage.save_snapshot(snapshot)
    jobs = manager.restore_jobs(sid)
    assert len(jobs) == 2


def test_resume_manager_restore_tasks_filtered(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[{"job_id": "j1"}],
        tasks=[{"task_id": "t1", "job_id": "j1"}, {"task_id": "t2", "job_id": "j2"}],
        artifacts=[], settings={}, version="0.0.0.0",
    )
    sid = storage.save_snapshot(snapshot)
    tasks = manager.restore_tasks(sid, job_id="j1")
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "t1"


def test_resume_manager_selective_restore(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[{"job_id": "j1"}, {"job_id": "j2"}],
        tasks=[{"task_id": "t1", "job_id": "j1"}],
        artifacts=[], settings={}, version="0.0.0.0",
    )
    sid = storage.save_snapshot(snapshot)
    result = manager.selective_restore(sid, job_ids=["j1"])
    assert len(result["jobs"]) == 1
    assert len(result["tasks"]) == 1


def test_resume_manager_verify_invalid_snapshot(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.0.0")
    sid = storage.save_snapshot(snapshot)
    # Tamper with the file directly
    path = storage._path(sid)
    data = path.read_text(encoding="utf-8")
    path.write_text(data.replace('"jobs": []', '"jobs": [{"tampered": true}]'), encoding="utf-8")
    assert manager.verify_snapshot(sid) is False
