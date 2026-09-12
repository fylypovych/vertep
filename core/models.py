from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

from .configuration import SAFE_ID

class JobStatus(str, Enum):
    NEW = "NEW"
    WAITING_FOR_SYSTEM = "WAITING_FOR_SYSTEM"
    SCRIPT_QUEUED = "SCRIPT_QUEUED"
    SCRIPT_GENERATING = "SCRIPT_GENERATING"
    SCRIPT_PENDING_APPROVAL = "SCRIPT_PENDING_APPROVAL"
    SCRIPT_REVISION_REQUESTED = "SCRIPT_REVISION_REQUESTED"
    SCRIPT_APPROVED = "SCRIPT_APPROVED"
    SCRIPT_FAILED = "SCRIPT_FAILED"
    STORYBOARD_QUEUED = "STORYBOARD_QUEUED"
    STORYBOARD_GENERATING = "STORYBOARD_GENERATING"
    STORYBOARD_PENDING_APPROVAL = "STORYBOARD_PENDING_APPROVAL"
    STORYBOARD_REVISION_REQUESTED = "STORYBOARD_REVISION_REQUESTED"
    STORYBOARD_APPROVED = "STORYBOARD_APPROVED"
    STORYBOARD_REJECTED = "STORYBOARD_REJECTED"
    STORYBOARD_FAILED = "STORYBOARD_FAILED"
    SCRIPTING = "SCRIPTING"
    SCRIPT_READY = "SCRIPT_READY"
    ASSET_GENERATION = "ASSET_GENERATION"
    ASSETS_READY = "ASSETS_READY"
    VIDEO_GENERATION = "VIDEO_GENERATION"
    VIDEO_READY = "VIDEO_READY"
    VIDEO_PENDING_APPROVAL = "VIDEO_PENDING_APPROVAL"
    VIDEO_REVISION_REQUESTED = "VIDEO_REVISION_REQUESTED"
    VIDEO_APPROVED = "VIDEO_APPROVED"
    VIDEO_FAILED = "VIDEO_FAILED"
    ASSEMBLY = "ASSEMBLY"
    TTS_GENERATING = "TTS_GENERATING"
    TTS_READY = "TTS_READY"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    READY = "READY"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


JOB_STATE_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.NEW: {JobStatus.SCRIPT_QUEUED, JobStatus.WAITING_FOR_SYSTEM, JobStatus.CANCELLED, JobStatus.PAUSED},
    JobStatus.WAITING_FOR_SYSTEM: {JobStatus.SCRIPT_QUEUED, JobStatus.CANCELLED},
    JobStatus.SCRIPT_QUEUED: {JobStatus.SCRIPT_GENERATING, JobStatus.CANCELLED},
    JobStatus.SCRIPT_GENERATING: {JobStatus.SCRIPT_PENDING_APPROVAL, JobStatus.SCRIPT_FAILED, JobStatus.CANCELLED},
    JobStatus.SCRIPT_PENDING_APPROVAL: {JobStatus.SCRIPT_APPROVED, JobStatus.SCRIPT_REVISION_REQUESTED, JobStatus.SCRIPT_FAILED, JobStatus.CANCELLED},
    JobStatus.SCRIPT_REVISION_REQUESTED: {JobStatus.SCRIPT_GENERATING, JobStatus.CANCELLED},
    JobStatus.SCRIPT_APPROVED: {JobStatus.STORYBOARD_QUEUED, JobStatus.CANCELLED},
    JobStatus.SCRIPT_FAILED: {JobStatus.SCRIPT_QUEUED, JobStatus.CANCELLED},
    JobStatus.STORYBOARD_QUEUED: {JobStatus.STORYBOARD_GENERATING, JobStatus.CANCELLED},
    JobStatus.STORYBOARD_GENERATING: {JobStatus.STORYBOARD_PENDING_APPROVAL, JobStatus.STORYBOARD_FAILED, JobStatus.CANCELLED},
    JobStatus.STORYBOARD_PENDING_APPROVAL: {JobStatus.STORYBOARD_APPROVED, JobStatus.STORYBOARD_REVISION_REQUESTED, JobStatus.STORYBOARD_FAILED, JobStatus.CANCELLED},
    JobStatus.STORYBOARD_REVISION_REQUESTED: {JobStatus.STORYBOARD_GENERATING, JobStatus.CANCELLED},
    JobStatus.STORYBOARD_APPROVED: {JobStatus.ASSET_GENERATION, JobStatus.CANCELLED},
    JobStatus.STORYBOARD_REJECTED: {JobStatus.CANCELLED, JobStatus.STORYBOARD_QUEUED},
    JobStatus.STORYBOARD_FAILED: {JobStatus.STORYBOARD_QUEUED, JobStatus.CANCELLED},
    JobStatus.ASSET_GENERATION: {JobStatus.ASSETS_READY, JobStatus.TTS_GENERATING, JobStatus.FAILED, JobStatus.PAUSED, JobStatus.CANCELLED},
    JobStatus.ASSETS_READY: {JobStatus.TTS_GENERATING, JobStatus.VIDEO_GENERATION, JobStatus.PAUSED, JobStatus.CANCELLED},
    JobStatus.VIDEO_GENERATION: {JobStatus.VIDEO_PENDING_APPROVAL, JobStatus.VIDEO_FAILED, JobStatus.CANCELLED},
    JobStatus.TTS_GENERATING: {JobStatus.TTS_READY, JobStatus.FAILED, JobStatus.PAUSED, JobStatus.CANCELLED},
    JobStatus.TTS_READY: {JobStatus.VIDEO_GENERATION, JobStatus.ASSEMBLY, JobStatus.PAUSED, JobStatus.CANCELLED},
    JobStatus.VIDEO_PENDING_APPROVAL: {JobStatus.VIDEO_APPROVED, JobStatus.VIDEO_REVISION_REQUESTED, JobStatus.VIDEO_FAILED, JobStatus.CANCELLED},
    JobStatus.VIDEO_REVISION_REQUESTED: {JobStatus.VIDEO_GENERATION, JobStatus.CANCELLED},
    JobStatus.VIDEO_APPROVED: {JobStatus.ASSEMBLY, JobStatus.VIDEO_READY, JobStatus.CANCELLED},
    JobStatus.VIDEO_FAILED: {JobStatus.VIDEO_GENERATION, JobStatus.CANCELLED},
    JobStatus.ASSEMBLY: {JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.PENDING_APPROVAL: {JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.READY: {JobStatus.PUBLISHING, JobStatus.PAUSED, JobStatus.CANCELLED},
    JobStatus.PUBLISHING: {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.PUBLISHED: {JobStatus.CANCELLED},
    JobStatus.FAILED: {JobStatus.NEW, JobStatus.SCRIPT_QUEUED, JobStatus.STORYBOARD_QUEUED, JobStatus.VIDEO_GENERATION, JobStatus.TTS_GENERATING, JobStatus.CANCELLED},
    JobStatus.PAUSED: {JobStatus.NEW, JobStatus.CANCELLED},
    JobStatus.CANCELLED: set(),
}


def job_transition_allowed(previous: JobStatus | str, current: JobStatus | str) -> bool:
    return current in JOB_STATE_TRANSITIONS.get(JobStatus(previous), set())


CHANNEL_TYPES = {"telegram", "youtube", "facebook", "tiktok", "instagram", "threads"}


class Channel(BaseModel):
    channel_id: str = Field(min_length=1, max_length=64)
    brand_id: str = Field(pattern=SAFE_ID.pattern)
    channel_type: str
    target: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = Field(default_factory=dict)


class ChannelCreate(BaseModel):
    brand_id: str = Field(pattern=SAFE_ID.pattern)
    channel_type: str
    target: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)


class ChannelUpdate(BaseModel):
    target: str | None = Field(default=None, min_length=1, max_length=256)
    enabled: bool | None = None
    metadata: dict | None = None


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


class StoryboardScene(BaseModel):
    index: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=4000)
    video_prompt: str = Field(min_length=1, max_length=4000)
    voiceover: str = Field(default="", max_length=10000)
    duration: float = Field(gt=0, le=600)
    scene_id: str | None = None
    artifact_id: str | None = None
    image_prompt: str | None = None
    image_prompt_history: list[dict] = Field(default_factory=list)
    image_artifact_id: str | None = None
    image_version: int | None = None


class StoryboardVersion(BaseModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=100)
    scenes: list[StoryboardScene] = Field(min_length=1, max_length=200)
    prompt_version: str = "1"
    model: str = ""
    status: str = Field(pattern="^(pending_approval|approved|rejected|superseded)$")
    revision_request: str | None = Field(default=None, max_length=4000)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: str | None = None
    decided_by: str | None = None
    image_version: int = 1
    image_status: str = Field(default="pending", pattern="^(pending|generating|ready|approved|superseded)$")
    approved_script: dict[str, Any] | None = Field(default=None)

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
    topic: str | None = None

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
    approved_channels: list[str] = Field(default_factory=list)
    approval_status: str = "pending"
    published_to: list[str] = Field(default_factory=list)
    publication_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    task_type: str = "image"
    min_vram_mb: int = 0
    max_retries: int = 3
    brand_id: str = "brand01"
    workflow: str | None = None
    active_task_id: str | None = None
    active_task_ids: dict[str, str] = Field(default_factory=dict)
    tts_active_task_ids: dict[str, str] = Field(default_factory=dict)
    completed_task_ids: list[str] = Field(default_factory=list)
    aspect_ratio: str = "16:9"
    output_preset: str = "youtube"
    version: int = 1
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    scenes: list[SceneRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    scheduled_for: str | None = None
    storyboards: list[StoryboardVersion] = Field(default_factory=list)
    active_storyboard_version: int | None = None
    active_image_version: int | None = None
    storyboard_attempt: int = 0
    storyboard_error: str | None = None
    storyboard_revision_chat_id: str | None = None
    storyboard_revision_version: int | None = None
    script_revision_chat_id: str | None = None
    script_revision_pending: bool = False
    video_revision_chat_id: str | None = None
    video_revision_pending: bool = False
    image_storyboard_task_ids: dict[str, str] = Field(default_factory=dict)
    image_storyboard_task_versions: dict[str, dict[str, int]] = Field(default_factory=dict)
    image_storyboard_error: str | None = None
    storyboard_task_id: str | None = None
    script_task_id: str | None = None
    script_attempt: int = 0
    script_error: str | None = None
    publish_task_ids: dict[str, str] = Field(default_factory=dict)
    publish_attempt: int = 0
    publish_error: str | None = None

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
    voice_catalog: dict[str, Any] = Field(default_factory=dict)

class TaskClaim(BaseModel):
    node_name: str
    gpu_name: str = "unknown"
    vram_mb: int = 0
    free_vram_mb: int | None = None
    supported_tasks: list[str] = Field(default_factory=lambda: ["image"])
    supported_workflows: list[str] = Field(default_factory=lambda: ["*"])
    capabilities: list[str] = Field(default_factory=lambda: ["image_generation"])
    voice_catalog: dict[str, Any] = Field(default_factory=dict)


class NodeAction(BaseModel):
    action: str = Field(pattern="^(drain|resume|quarantine|unquarantine|self-test|disable|enable|restart|logs|update|rotate|revoke)$")
    reason: str = Field(default="", max_length=500)


class IntegrationSecretUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=16_384)


class TelegramSetup(BaseModel):
    public_url: str | None = Field(default=None, description="Legacy webhook URL — polling is now the default transport")
    webhook_secret: str | None = None
    allowed_chat_ids: str | None = None
    admin_chat_ids: str | None = None


class RollingUpdateRequest(BaseModel):
    target_version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9][0-9A-Za-z.+-]*$")
    node_ids: list[str] = Field(min_length=1, max_length=1000)
    order: str = Field(default="workers-first", pattern=r"^(workers-first|core-first|custom)$")
    update_timeout_seconds: int = Field(default=600, ge=60, le=86400)
    canary: bool = Field(default=False)

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
    images: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)

class WorkerLogBatch(BaseModel):
    node_name: str
    entries: list[dict[str, Any]] = Field(max_length=500)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
