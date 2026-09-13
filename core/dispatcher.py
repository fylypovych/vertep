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


def _voice_ready(worker: dict, requirements: dict | None) -> bool:
    if not requirements:
        return True
    catalog = worker.get("voice_catalog") or {}
    plural = {"voice": "voices", "model": "models"}
    for key, required in requirements.items():
        if not required:
            continue
        supported = catalog.get(plural.get(key, key))
        if not supported:
            continue  # no catalog advertised → assume the node can handle it
        if "*" in supported or required in supported:
            continue
        return False
    return True


def compute_load_score(worker: dict) -> float:
    """Normalized 0..1 composite load score for a worker.

    Combines GPU load (percent), CPU load (fraction) and an inflight penalty so
    the dispatcher can rank equally-capable nodes and favour the least-loaded
    one (locality/load-aware scheduling).  Unknown/absent metrics are treated
    as zero so a healthy but metric-free node is scored more favourably than a
    node that demonstrably reports load.
    """
    try:
        gpu = min(1.0, max(0.0, float(worker.get("gpu_load") or 0) / 100.0))
    except (TypeError, ValueError):
        gpu = 0.0
    try:
        cpu = min(1.0, max(0.0, float(worker.get("cpu_load") or 0)))
    except (TypeError, ValueError):
        cpu = 0.0
    busy = 1.0 if (worker.get("current_task") or worker.get("status") == "BUSY") else 0.0
    return round(0.5 * gpu + 0.3 * cpu + 0.2 * busy, 4)


def _satisfies_locality(worker: dict, job: Job) -> bool:
    """Locality: a worker must expose every ``scheduler_tags`` the job requires.

    Tag affinity routes a job that pins to specific nodes (e.g. a node hosting
    a particular model/comfyui backend) to only those workers.  When a job
    declares no required tags, any capable node is a locality candidate.
    """
    required = set(job.required_tags or [])
    if not required:
        return True
    tags = set(worker.get("scheduler_tags") or [])
    return required.issubset(tags)


def available_worker(workers: list[dict], job: Job, task_type: str | None = None, min_vram_mb: int | None = None,
                     voice_requirements: dict | None = None) -> dict | None:
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
            continue
        available_vram = worker.get("free_vram_mb")
        if available_vram is None:
            available_vram = worker.get("vram_mb", 0)
        required_vram = min_vram_mb if min_vram_mb is not None else job.min_vram_mb
        if available_vram < required_vram:
            continue
        effective_task_type = task_type or job.task_type
        required_capability = {"image": "image_generation", "video": "video_generation",
                               "text": "text_generation", "voice": "speech_synthesis",
                               "publish": "publishing"}.get(effective_task_type, effective_task_type)
        capabilities = tested_capabilities if require_self_test else set(worker.get("capabilities") or [])
        if capabilities and required_capability not in capabilities:
            continue
        if not capabilities and effective_task_type not in worker.get("supported_tasks", ["image"]):
            continue
        supported_workflows = worker.get("supported_workflows", ["*"])
        if "*" not in supported_workflows and (job.workflow or "") not in supported_workflows:
            continue
        if effective_task_type == "voice" and voice_requirements and not _voice_ready(worker, voice_requirements):
            continue
        if not _satisfies_locality(worker, job):
            continue
        worker["load_score"] = compute_load_score(worker)
        candidates.append(worker)
    if not candidates:
        return None
    # Prefer explicitly configured priority, then the lowest composite load
    # score (load-aware), then the most free VRAM, then fair-share scheduling
    # (fewest prior dispatches). Node name makes equal scores deterministic.
    return min(candidates, key=lambda worker: (
        -int(worker.get("scheduler_priority", 0)),
        float(worker.get("load_score", 0.0)),
        -int(worker.get("free_vram_mb") if worker.get("free_vram_mb") is not None
             else worker.get("vram_mb", 0)),
        int(worker.get("scheduler_dispatch_count", 0)),
        str(worker.get("node_name", "")),
    ))


def can_retry(job: Job) -> bool:
    return job.retries < job.max_retries
