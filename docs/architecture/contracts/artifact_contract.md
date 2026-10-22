Artifact Contract
=================

1. Структура artifact metadata (type, format, checksum, references)
---------------------------------
```yaml
artifact:
  id: "unique-artifact-id"
  type: "script|video|image|model|audio|log"
  format: "markdown|mp4|png|gguf|wav|txt"
  size_bytes: 12345
  checksum:
    algorithm: "sha256"
    value: "abc123..."
  created_at: "2024-01-15T10:30:00Z"
  owner: "worker_id|core"
  references: {
    "source_task": "task_123",
    "parent_job": "job_456"
  }
```

2. Контракт artifact storage (upload, download, verify, cache)
---------------------------------
- `upload(artifact_id, data, checksum)` → {stored, checksum_verified}
- `download(artifact_id)` → {data, checksum_valid}
- `verify(artifact_id, checksum)` → {valid: true|false, stored_checksum, computed_checksum}
- `cache(artifact_id, ttl)` → artifact kept in cache for TTL seconds

3. Контракт artifact lifecycle (create, claim, transfer, delete)
---------------------------------
- `create(artifact_definition)` → artifact_id
- `claim(artifact_id, worker_id)` → {claimed, transfer_token}
- `transfer(artifact_id, from_worker, to_worker)` → {transferred}
- `delete(artifact_id)` → {deleted, cleanup_required}

4. Контракт cross-node artifact access (URL, stream, local cache)
---------------------------------
- Access via signed URL with expiration.
- Stream resumption support for large artifacts.
- Local cache on worker: path format `/opt/vertep/storage/{artifact_id}/{chunk}`.

5. Edge cases
-----------
- Checksum mismatch: upload rejected, artifact marked corrupted.
- Orphaned artifact: no owner → CORE cleanup after TTL.
- Concurrent access: only one worker can claim artifact at a time.
- Transfer failure: retry with exponential backoff, finalize as failed.
- Size limit: artifacts > 5GB require special handling (chunked transfer).