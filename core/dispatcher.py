from datetime import datetime, timezone
import os

from .models import Job, JobStatus

# Runtime readiness states that qualify a node for dispatch when emitted by its
# durable self-test record (enrollment separated from runtime readiness).
RUNTIME_READY_STATUSES = {"ONLINE", "READY", "FREE", "BUSY"}


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
    # Only explicitly attested capabilities count as tested.  Unconfirmed
    # (missing ``tested_capabilities``) must NOT silently fall back to the
    # declared set, otherwise declared-but-unverified capabilities become
    # dispatch-eligible.
    attested = set(worker.get("tested_capabilities") or [])
    return declared & attested


def runtime_ready(worker: dict) -> bool:
    """True when the durable self-test ``runtime_status`` does not disqualify a node.

    When a worker record carries a persisted ``runtime_status`` (the outcome of
    its role self-test), an explicitly-failed/pending node is never eligible for
    dispatch even if a stale heartbeat claims capability readiness.  Records that
    never carry the field are treated as runtime-ready so existing callers that
    build worker dicts without registry data keep working.
    """
    runtime_status = worker.get("runtime_status")
    return runtime_status is None or runtime_status in RUNTIME_READY_STATUSES


def _voice_ready(worker: dict, requirements: dict | None) -> bool:
    """Fail-closed readiness check for a voice requirement against a worker catalog.

    Issue i.0.0.1.5: a worker that *declares* a voice catalog must prove it
    supports the exact voice/model/language the Job requires.  A declared-but-
    empty catalog is not treated as "accepts anything": an empty ``voices``
    list means the node has no voices at all, so it is refused instead of
    silently trusted.  Only a worker that never advertised a catalog at all
    (backward compatible, pre-catalog workers) is assumed capable, because
    there is nothing to contradict.

    A requirement is satisfied when the catalog advertises the value, advertises
    the wildcard ``"*"``, or omits the key entirely (the worker did not claim to
    gate on that dimension).  Any other combination is unsupported and the node
    is skipped so the Job is not dispatched to a runtime that cannot honour it.
    """
    if not requirements:
        return True
    catalog = worker.get("voice_catalog")
    if catalog is None:
        # No catalog advertised at all: nothing to contradict, keep the node.
        return True
    if not catalog:
        # A declared-but-empty catalog means the node has no voices at all:
        # refuse instead of silently trusting it.  Only the absence of the
        # field (None) stays backward compatible.
        return False
    plural = {"voice": "voices", "model": "models", "language": "languages"}
    for key, required in requirements.items():
        if not required:
            continue
        catalog_key = plural.get(key, key)
        if catalog_key not in catalog:
            # The node did not claim to gate on this dimension: assume capable.
            continue
        supported = catalog.get(catalog_key)
        if not supported:
            # A declared-but-empty list means the node has none: refuse.
            return False
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


def role_runtime_blocks(worker: dict | str) -> bool:
    """True when a locally deployed role contract is measured as not ready.

    The role catalog in ``config/node_roles.json`` is only a declaration, so
    dispatch additionally consults the measured per-role runtime status.  A role
    that this installation claims to run (its own role or an additional role)
    but whose runtime is measured ``OFFLINE``/``DEGRADED`` must not keep
    attracting work.  Roles without evidence stay dispatchable: the per-node
    self-test guard remains the authoritative runtime gate.
    """
    from .role_runtime import role_blocks_dispatch

    role = worker if isinstance(worker, str) else worker.get("role")
    if not role:
        return False
    try:
        return role_blocks_dispatch(str(role))
    except Exception:
        return False


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
        if require_self_test and not runtime_ready(worker):
            continue
        if role_runtime_blocks(worker):
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
                               "publish": "publishing",
                               # Issue #122 P5: final assembly of an external engine
                               # runs on a Worker, so it is gated like any other task.
                               "assembly": "video_assembly"}.get(effective_task_type, effective_task_type)
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
