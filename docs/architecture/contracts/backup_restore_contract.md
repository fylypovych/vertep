Backup/Restore Contract
=======================

1. Структура CORE state snapshot (jobs, tasks, artifacts, settings)
---------------------------------
```json
{
  "snapshot_id": "unique-id",
  "version": "0.0.2.76",
  "created_at": "2024-01-15T10:30:00Z",
  "jobs": {
    "job_id_1": {
      "status": "COMPLETED",
      "tasks": ["task_1", "task_2"],
      "artifacts": ["artifact_1", "artifact_2"],
      "progress": 100
    }
  },
  "tasks": {
    "task_id_1": {
      "status": "COMPLETED",
      "artifact": "artifact_1",
      "progress": 100
    }
  },
  "artifacts": {
    "artifact_id_1": {
      "checksum": "sha256:abc123",
      "size_bytes": 12345,
      "location": "storage://..."
    }
  },
  "settings": {
    "node_role": "gpu",
    "capabilities": ["image_generation"],
    "configuration": {...}
  }
}

2. Контракт backup creation (incremental, full, checksumming)
---------------------------------
- `full_backup()` → {snapshot_id, size_bytes, checksum, created_at}
- `incremental_backup(since_snapshot_id)` → {snapshot_id, size_bytes, checksum, created_at, changes: [{type, id, old_value, new_value}]}
- All backups include checksum verification.
- Backup encrypted at rest with repository key.
- Backup metadata stored in `/opt/vertep/backups/metadata.json`.

3. Контракт restore (selective restore, resume jobs, artifact recovery)
---------------------------------
- `restore(snapshot_id)` → {success, restored_items, failed_items}
- Selective restore: restore specific jobs/tasks/artifacts by ID.
- Resume jobs: restore job state and continue from last checkpoint.
- Artifact recovery: download from storage, verify checksum, place in correct location.

4. Контракт module-specific backup (declarative backup requirements)
---------------------------------
- Each module can declare backup requirements in its manifest.
- Required backup items: {type: "database|file|registry|custom", path/identifier, frequency}
- CORE respects module backup requirements during full/incremental backup cycles.
- Missing required backup items: backup marked incomplete, operator notified.

5. Edge cases
-----------
- Inconsistent state: backup includes partial data, checksum mismatch → retry.
- Storage full: backup deferred, operator notified, system enters degraded mode.
- Concurrent backups: only one full backup at a time, incrementals allowed.
- Restore to different version: compatibility check, may require migration steps.
- Corrupted backup: detected via checksum, automatically rejected, next backup used.