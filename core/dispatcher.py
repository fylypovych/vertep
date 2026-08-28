from datetime import datetime, timezone
import os

from .models import Job, JobStatus


def current_tested_capabilities(worker: dict, now: datetime | None = None) -> set[str]:
    """Return only capabilities covered by a recent successful role self-test."""
    self_test = worker.get("self_test") or {}
    if self_test.get("status") != "PASSED" or self_test.get("role") != worker.get("role"):
        return set()
    try:
        checked_at = datetime.fromisoformat(str(self_test["checked_at"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            return set()
        age = ((now or datetime.now(timezone.utc)) - checked_at.astimezone(timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError):
        return set()
    maximum_age = int(os.getenv("WORKER_SELF_TEST_MAX_AGE_SECONDS", "900"))
    maximum_clock_skew = int(os.getenv("WORKER_CLOCK_SKEW_SECONDS", "300"))
    if age > maximum_age or age < -maximum_clock_skew:
        return set()
    declared = set(worker.get("capabilities") or [])
    attested = set(worker.get("tested_capabilities") or declared)
    return declared & attested


def available_worker(workers: list[dict], job: Job) -> dict | None:
    now = datetime.now(timezone.utc)
    candidates: list[dict] = []
    for worker in workers:
        last_seen = worker.get("last_seen")
        if not last_seen:
            continue
        if (now - datetime.fromisoformat(last_seen)).total_seconds() > 45:
            continue
        if worker.get("status") not in {"ONLINE", "FREE", "READY"}:
            continue
        require_self_test = os.getenv("REQUIRE_WORKER_SELF_TEST", "false").lower() == "true"
        tested_capabilities = current_tested_capabilities(worker, now)
        if require_self_test and not tested_capabilities:
        if (worker.get("self_test", {}).get("status") != "PASSED"
                and os.getenv("REQUIRE_WORKER_SELF_TEST", "false").lower() == "true"):
            continue
        available_vram = worker.get("free_vram_mb")
        if available_vram is None:
            available_vram = worker.get("vram_mb", 0)
        if available_vram < job.min_vram_mb:
            continue
        required_capability = {"image": "image_generation", "video": "video_generation",
                               "text": "text_generation", "voice": "speech_synthesis",
                               "publish": "publishing"}.get(job.task_type, job.task_type)
        capabilities = tested_capabilities if require_self_test else set(worker.get("capabilities") or [])
        capabilities = worker.get("capabilities") or []
        if capabilities and required_capability not in capabilities:
            continue
        if not capabilities and job.task_type not in worker.get("supported_tasks", ["image"]):
            continue
        supported_workflows = worker.get("supported_workflows", ["*"])
        if "*" not in supported_workflows and (job.workflow or "") not in supported_workflows:
            continue
        candidates.append(worker)
    if not candidates:
        return None
    # Prefer explicitly configured priority, then lower runtime load and more
    # free VRAM. Node name makes equal scores deterministic and fair to debug.
    return min(candidates, key=lambda worker: (
        -int(worker.get("scheduler_priority", 0)),
        float(worker.get("gpu_load") or 0),
        -int(worker.get("free_vram_mb") if worker.get("free_vram_mb") is not None
             else worker.get("vram_mb", 0)),
        str(worker.get("node_name", "")),
    ))


def can_retry(job: Job) -> bool:
    return job.retries < job.max_retries
