"""Tests for module-specific backup and workflow checkpoint integration (Stage 8, Issue #300 i.0.0.3.0).

Tests that module state and workflow checkpoints can be saved to and
restored from backup snapshots.
"""

from core.backup.resume import ResumeManager
from core.backup.snapshot import Snapshot
from core.backup.storage import SnapshotStorage


def test_backup_save_and_restore_module_state(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path / "backups"))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.3.0",
    )
    sid = storage.save_snapshot(snapshot)

    manager.save_module_state(sid, "text", {"cached_prompts": 42, "version": "1.0"})
    manager.save_module_state(sid, "image", {"cached_models": ["sd-v1"], "gpu_memory_mb": 6144})

    assert "text" in manager.list_modules_with_state(sid)
    assert "image" in manager.list_modules_with_state(sid)

    text_state = manager.restore_module_state(sid, "text")
    assert text_state == {"cached_prompts": 42, "version": "1.0"}
    image_state = manager.restore_module_state(sid, "image")
    assert image_state == {"cached_models": ["sd-v1"], "gpu_memory_mb": 6144}

    # Missing module returns None
    assert manager.restore_module_state(sid, "audio") is None


def test_backup_save_and_restore_workflow_checkpoint(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path / "backups"))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.3.0",
    )
    sid = storage.save_snapshot(snapshot)

    checkpoint = {
        "job_id": "job-wf-001",
        "status": "IN_PROGRESS",
        "tasks": {"t1": {"status": "COMPLETED"}, "t2": {"status": "PENDING"}},
        "progress": 50,
    }
    manager.save_workflow_checkpoint(sid, "job-wf-001", checkpoint)

    assert "job-wf-001" in manager.list_workflow_checkpoints(sid)
    restored = manager.restore_workflow_checkpoint(sid, "job-wf-001")
    assert restored == checkpoint

    # Missing job returns None
    assert manager.restore_workflow_checkpoint(sid, "no-such-job") is None


def test_backup_snapshot_includes_module_and_workflow_fields(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path / "backups"))
    snapshot = Snapshot.create(
        jobs=[{"job_id": "j1"}], tasks=[], artifacts=[], settings={},
        version="0.0.3.0",
        module_state={"text": {"key": "value"}},
        workflow_checkpoints={"job-1": {"status": "READY"}},
    )
    sid = storage.save_snapshot(snapshot)
    loaded = storage.load_snapshot(sid)
    assert loaded.module_state == {"text": {"key": "value"}}
    assert loaded.workflow_checkpoints == {"job-1": {"status": "READY"}}
    assert loaded.verify() is True


def test_backup_snapshot_checksum_changes_with_module_state(tmp_path):
    storage = SnapshotStorage(root=str(tmp_path / "backups"))
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version="0.0.3.0",
    )
    checksum_before = snapshot.checksum

    snapshot.module_state["text"] = {"data": 1}
    snapshot.checksum = snapshot.compute_checksum()
    assert snapshot.checksum != checksum_before
