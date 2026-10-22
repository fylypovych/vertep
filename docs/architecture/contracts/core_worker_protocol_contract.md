CORE ↔ Worker Protocol Contract
================================

1. Контракт task assignment (TaskRequest, TaskResponse)
---------------------------------
```json
{
  "task_request": {
    "task_id": "unique-task-id",
    "job_id": "unique-job-id",
    "role": "gpu|text|voice|publisher|core",
    "task_type": "image_generation|video_generation|text_generation|voice_synthesis|publishing|video_assembly",
    "definition": {
      "name": "string",
      "prompt": "string",
      "character_config": {...},
      "parameters": {...}
    },
    "capabilities_required": ["image_generation"],
    "vram_mb": 8192,
    "timeout_minutes": 60,
    "lease_duration_minutes": 30,
    "approval_required": false
  },
  "task_response": {
    "accepted": true|false,
    "declined_reason": "string (optional)",
    "worker_id": "string",
    "capabilities_matched": ["image_generation"]
  }
}
```

2. Контракт task lifecycle (TaskClaim, TaskProgress, TaskComplete, TaskFail, TaskCancel)
---------------------------------
- `TaskClaim`: worker claims task → status changes to "IN_PROGRESS", lease starts
- `TaskProgress`: worker reports progress → {task_id, progress_percent, output_artifact_ref, error: optional}
- `TaskComplete`: worker finishes task → {task_id, result_artifact, execution_time, success: true|false}
- `TaskFail`: task fails → {task_id, error_type, error_message, retryable: true|false}
- `TaskCancel`: task cancelled → {task_id, cancelled_by: "worker"|"core", cleanup_required: true|false}

3. Контракт worker registration (WorkerRegister, WorkerCapabilities)
---------------------------------
- `WorkerRegister`: {worker_id, role, capabilities, hardware: {gpu:..., cpu:..., ram:...}, version, software: {...}}
- `WorkerCapabilities`: periodic heartbeat with updated {capabilities, resources, status, last_heartbeat}
- Registration must include CSR (Certificate Signing Request) for mTLS.
- Capabilities must match declared `tested_capabilities` from self-test.

4. Контракт heartbeat/lease renewal (Heartbeat, LeaseRenewal, LeaseRevocation)
---------------------------------
- Heartbeat sent every 30 seconds.
- Lease renewal: worker extends lease before expiry → CORE updates lease timeout.
- Lease revocation: CORE can revoke lease → worker must immediately release task.
- Heartbeat includes: worker_id, status, current_task_id, vram_usage, cpu_usage.

5. Версіонування протоколу (ProtocolVersion, backward compatibility)
---------------------------------
- Protokol version: semver format, e.g., "1.0.0".
- Backward compatibility: new protocol version must support old message formats.
- Deprecation: old protocol versions marked deprecated after 2 release cycles.
- Compatibility matrix: CORE and worker must have compatible protocol versions.

6. Edge cases
-----------
- Stale heartbeat: lease expired → task revoked, reassigned to another worker.
- Duplicate task claim: second worker rejected, first worker retains claim.
- Missing capability: task reassigned to worker with required capability.
- Worker offline during task: CORE revokes lease, marks task as failed.
- Heartbeat flood: CORE ignores heartbeats exceeding max frequency.