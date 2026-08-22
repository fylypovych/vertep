from datetime import datetime, timezone

from .models import Job, JobStatus


def available_worker(workers: list[dict], job: Job) -> dict | None:
    now = datetime.now(timezone.utc)
    for worker in workers:
        last_seen = worker.get("last_seen")
        if not last_seen:
            continue
        if (now - datetime.fromisoformat(last_seen)).total_seconds() > 45:
            continue
        if worker.get("status") != "ONLINE":
            continue
        available_vram = worker.get("free_vram_mb")
        if available_vram is None:
            available_vram = worker.get("vram_mb", 0)
        if available_vram < job.min_vram_mb:
            continue
        if job.task_type not in worker.get("supported_tasks", ["image"]):
            continue
        supported_workflows = worker.get("supported_workflows", ["*"])
        if "*" not in supported_workflows and (job.workflow or "") not in supported_workflows:
            continue
        return worker
    return None


def can_retry(job: Job) -> bool:
    return job.retries < job.max_retries
