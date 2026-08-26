from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

class JobStatus(str, Enum):
    NEW = "NEW"
    WAITING_FOR_SYSTEM = "WAITING_FOR_SYSTEM"
    SCRIPTING = "SCRIPTING"
    SCRIPT_READY = "SCRIPT_READY"
    ASSET_GENERATION = "ASSET_GENERATION"
    ASSETS_READY = "ASSETS_READY"
    VIDEO_GENERATION = "VIDEO_GENERATION"
    VIDEO_READY = "VIDEO_READY"
    ASSEMBLY = "ASSEMBLY"
    READY = "READY"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class WorkerState(str, Enum):
    ENROLLING = "ENROLLING"
    SELF_TESTING = "SELF_TESTING"
    READY = "READY"
    BUSY = "BUSY"
    DRAINING = "DRAINING"
    UPDATING = "UPDATING"
    RECOVERING = "RECOVERING"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"
    QUARANTINED = "QUARANTINED"
    REVOKED = "REVOKED"
    # Accepted during the rolling protocol migration; Core normalizes both to READY.
    ONLINE = "ONLINE"
    FREE = "FREE"


WORKER_STATE_TRANSITIONS: dict[WorkerState, set[WorkerState]] = {
    WorkerState.ENROLLING: {WorkerState.SELF_TESTING, WorkerState.READY, WorkerState.ERROR},
    WorkerState.SELF_TESTING: {WorkerState.SELF_TESTING, WorkerState.READY, WorkerState.ERROR},
    WorkerState.READY: {WorkerState.READY, WorkerState.BUSY, WorkerState.DRAINING,
                        WorkerState.UPDATING, WorkerState.ERROR},
    WorkerState.BUSY: {WorkerState.BUSY, WorkerState.READY, WorkerState.DRAINING, WorkerState.ERROR},
    WorkerState.DRAINING: {WorkerState.DRAINING, WorkerState.READY, WorkerState.UPDATING,
                           WorkerState.ERROR},
    WorkerState.UPDATING: {WorkerState.UPDATING, WorkerState.RECOVERING,
                           WorkerState.SELF_TESTING, WorkerState.ERROR},
    WorkerState.RECOVERING: {WorkerState.RECOVERING, WorkerState.SELF_TESTING,
                             WorkerState.READY, WorkerState.ERROR},
    WorkerState.OFFLINE: {WorkerState.ENROLLING, WorkerState.SELF_TESTING,
                          WorkerState.READY, WorkerState.ERROR},
    WorkerState.ERROR: {WorkerState.ERROR, WorkerState.SELF_TESTING, WorkerState.READY,
                        WorkerState.UPDATING},
    WorkerState.QUARANTINED: {WorkerState.QUARANTINED},
    WorkerState.REVOKED: {WorkerState.REVOKED},
}


def normalized_worker_state(value: WorkerState | str) -> WorkerState:
    state = WorkerState(value)
    return WorkerState.READY if state in {WorkerState.ONLINE, WorkerState.FREE} else state


def worker_transition_allowed(previous: WorkerState | str, current: WorkerState | str) -> bool:
    previous_state = normalized_worker_state(previous)
    current_state = normalized_worker_state(current)
    return current_state in WORKER_STATE_TRANSITIONS.get(previous_state, set())

class StageName(str, Enum):
    SCRIPT = "SCRIPT"
    ASSETS = "ASSETS"
    TTS = "TTS"
    ASSEMBLY = "ASSEMBLY"
    PUBLISH = "PUBLISH"

class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"

class AttemptRecord(BaseModel):
    attempt: int
    status: str
    started_at: str
    completed_at: str | None = None
    node_name: str | None = None
    error: str | None = None

class StageRecord(BaseModel):
    name: StageName
    status: StageStatus = StageStatus.PENDING
    attempts: list[AttemptRecord] = Field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None

class SceneRecord(BaseModel):
    scene_id: str
    index: int
    prompt: str
    video_prompt: str | None = None
    voiceover: str = ""
    duration: float = Field(default=5, gt=0, le=600)
    status: StageStatus = StageStatus.PENDING
    task_id: str | None = None
    assigned_worker: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    attempts: list[AttemptRecord] = Field(default_factory=list)

class ArtifactRecord(BaseModel):
    artifact_id: str
    kind: str
    path: str
    filename: str
    mime_type: str
    size: int = Field(ge=0)
    sha256: str
    scene_id: str | None = None
    task_id: str | None = None
    node_name: str | None = None
    workflow: str | None = None
    created_at: str

class JobCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    character_id: str = Field(default="did_samogon", pattern="^[a-z0-9][a-z0-9_-]{1,63}$")
    priority: int = Field(default=5, ge=1, le=10)
    source: str = "web"
    task_type: str = "image"
    min_vram_mb: int = Field(default=0, ge=0)
    brand_id: str = Field(default="brand01", pattern="^[a-z0-9][a-z0-9_-]{1,63}$")
    workflow: str | None = None
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16)$")
    output_preset: str = "youtube"
    scheduled_for: str | None = None

class JobUpdate(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)
    script: dict[str, Any] | None = None
    prompt: str | None = None
    character_id: str | None = None
    priority: int | None = Field(default=None, ge=1, le=10)
    workflow: str | None = None

class Job(BaseModel):
    job_id: str
    topic: str
    character_id: str
    priority: int
    status: JobStatus
    created_at: str
    script: dict[str, Any] | None = None
    events: list[str] = Field(default_factory=list)
    output_path: str | None = None
    retries: int = 0
    source: str = "web"
    assigned_worker: str | None = None
    approved: bool = False
    published_to: list[str] = Field(default_factory=list)
    publication_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    task_type: str = "image"
    min_vram_mb: int = 0
    max_retries: int = 3
    brand_id: str = "brand01"
    workflow: str | None = None
    active_task_id: str | None = None
    active_task_ids: dict[str, str] = Field(default_factory=dict)
    completed_task_ids: list[str] = Field(default_factory=list)
    aspect_ratio: str = "16:9"
    output_preset: str = "youtube"
    version: int = 1
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    scenes: list[SceneRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    scheduled_for: str | None = None

class WorkerHeartbeat(BaseModel):
    node_name: str
    gpu_name: str = "demo"
    gpu_count: int = 1
    vram_mb: int = 0
    cuda_version: str = "demo"
    driver_version: str | None = None
    compute_capability: str | None = None
    gpu_architecture: str | None = None
    gpu_profile: str | None = None
    status: WorkerState = WorkerState.READY
    current_job: str | None = None
    current_task: str | None = None
    temperature: float | None = None
    gpu_load: float | None = None
    free_vram_mb: int | None = None
    last_seen: str | None = None
    supported_tasks: list[str] = Field(default_factory=lambda: ["image"])
    supported_workflows: list[str] = Field(default_factory=lambda: ["*"])
    role: str = "gpu"
    capabilities: list[str] = Field(default_factory=lambda: ["image_generation"])
    tested_capabilities: list[str] = Field(default_factory=list)
    version: str | None = None
    ram_mb: int | None = None
    disk_free_mb: int | None = None
    cpu_load: float | None = None
    runtime_version: str | None = None
    self_test: dict[str, Any] = Field(default_factory=dict)

class TaskClaim(BaseModel):
    node_name: str
    gpu_name: str = "unknown"
    vram_mb: int = 0
    free_vram_mb: int | None = None
    supported_tasks: list[str] = Field(default_factory=lambda: ["image"])
    supported_workflows: list[str] = Field(default_factory=lambda: ["*"])
    capabilities: list[str] = Field(default_factory=lambda: ["image_generation"])


class NodeAction(BaseModel):
    action: str = Field(pattern="^(drain|resume|quarantine|unquarantine|self-test)$")
    reason: str = Field(default="", max_length=500)


class IntegrationSecretUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=16_384)

class TaskRenew(BaseModel):
    node_name: str
    task_id: str

class TaskResult(BaseModel):
    job_id: str
    node_name: str
    task_id: str
    success: bool
    image_base64: str | None = None
    filename: str = "scene-001.png"
    error: str | None = None
    images: list[dict[str, str]] = Field(default_factory=list)
    artifacts: list[dict[str, str]] = Field(default_factory=list)

class WorkerLogBatch(BaseModel):
    node_name: str
    entries: list[dict[str, Any]] = Field(max_length=500)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
