"""Shared job helpers: result serialization, scheduling and scene lookup."""

import logging
import os
from datetime import datetime
from datetime import timezone
from functools import wraps

import httpx
from fastapi import Request

from ...models import TaskResult
from ...state import result_locks
from .._internal import _internal_auth_headers

logger = logging.getLogger(__package__)


MAX_VIDEO_ARTIFACT_BYTES = 268435456

# Attempt-scoped staging area of a Job directory (Issue #122 §6/P3): inputs that
# do not live inside the Job directory are copied here content-addressed, so a
# Worker receives Job-relative references and CORE paths never leave CORE.
ASSEMBLY_STAGING_DIR = ".assembly-staging"


def _max_video_artifact_bytes() -> int:
    return int(
        os.getenv(
            "MAX_VIDEO_ARTIFACT_BYTES",
            str(MAX_VIDEO_ARTIFACT_BYTES)))


def _serialize_job_result(function):
    @wraps(function)
    def locked(result: TaskResult, request: Request):
        with result_locks[result.job_id]:
            return function(result, request)
    return locked




def _job_is_due(job) -> bool:
    scheduler_url = os.getenv("SCHEDULER_URL", "").rstrip("/")
    if scheduler_url:
        try:
            response = httpx.post(
                f"{scheduler_url}/due",
                json={
                    "jobs": [
                        job.model_dump(
                            mode="json")],
                    "limit": 1},
                headers=_internal_auth_headers(),
                timeout=3)
            response.raise_for_status()
            return bool(response.json().get("jobs"))
        except (httpx.HTTPError, ValueError, AttributeError):
            # An unavailable scheduler must not stop due jobs: the rule is local too.
            logger.warning("Scheduler unavailable; using the local due check")
    if not job.scheduled_for:
        return True
    try:
        scheduled = datetime.fromisoformat(
            job.scheduled_for.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        return scheduled <= datetime.now(timezone.utc)
    except ValueError:
        return False


def _scene_for_task(job, task_id: str):
    scene_id = job.active_task_ids.get(
        task_id) or job.tts_active_task_ids.get(task_id)
    return next(
        (scene for scene in job.scenes if scene.scene_id == scene_id),
        None)
