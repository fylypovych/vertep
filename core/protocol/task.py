"""Task protocol definitions for CORE↔Worker communication.

Defines the contract for task assignment, lifecycle, and status reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Set


PROTOCOL_VERSION = "1.0.0"


def compatible_protocol(version: str) -> bool:
    import re
    return isinstance(version, str) and bool(re.fullmatch(r"1\.\d+\.\d+", version))


@dataclass
class TaskRequest:
    """Request to assign a task to a worker."""

    task_id: str
    job_id: str
    role: str  # "gpu"|"text"|"voice"|"publisher"|"core"
    task_type: str  # "image_generation"|"video_generation"|"text_generation"|"voice_synthesis"|"publishing"|"video_assembly"
    definition: dict  # Task-specific parameters (prompt, character_config, etc.)
    capabilities_required: Set[str] = field(default_factory=set)
    vram_mb: int = 4096
    timeout_minutes: int = 60
    lease_duration_minutes: int = 30
    approval_required: bool = False
    protocol_version: str = PROTOCOL_VERSION
    lease_id: str = ""


@dataclass
class TaskResponse:
    """Response from a worker regarding task assignment."""

    accepted: bool
    declined_reason: str = ""
    worker_id: str = ""
    capabilities_matched: Set[str] = field(default_factory=set)
    protocol_version: str = PROTOCOL_VERSION
    lease_id: str = ""


@dataclass
class TaskClaim:
    """Claim of a task by a worker."""

    task_id: str
    worker_id: str
    claimed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lease_expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=30))
    lease_id: str = ""
    protocol_version: str = PROTOCOL_VERSION


@dataclass
class TaskProgress:
    """Progress update from a worker on a task."""

    task_id: str
    worker_id: str
    progress_percent: float = 0.0
    output_artifact_ref: str = ""
    error: Optional[str] = None
    lease_id: str = ""
    protocol_version: str = PROTOCOL_VERSION


@dataclass
class TaskComplete:
    """Completion result from a worker for a task."""

    task_id: str
    worker_id: str
    result_artifact: str = ""
    execution_time_seconds: float = 0.0
    success: bool = True
    error: Optional[str] = None
    lease_id: str = ""
    protocol_version: str = PROTOCOL_VERSION


@dataclass
class TaskCancel:
    """Cancellation request for a task."""

    task_id: str
    cancelled_by: str  # "worker" or "core"
    immediate_cleanup: bool = True
    lease_id: str = ""
    protocol_version: str = PROTOCOL_VERSION


@dataclass
class TaskFail:
    task_id: str
    worker_id: str
    error_type: str
    error_message: str
    retryable: bool = False
    lease_id: str = ""
    protocol_version: str = PROTOCOL_VERSION
