import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import JobStatus


def cleanup_jobs(store, retention_days: int | None = None, dry_run: bool = True) -> dict:
    days = retention_days or int(os.getenv("JOB_RETENTION_DAYS", "30"))
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    terminal = {JobStatus.READY, JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED}
    candidates = []
    for job in list(store.jobs.values()):
        if job.status not in terminal or datetime.fromisoformat(job.created_at).timestamp() >= cutoff:
            continue
        candidates.append(job.job_id)
        if not dry_run:
            store.delete(job.job_id)
    return {"dry_run": dry_run, "retention_days": days, "jobs": candidates, "count": len(candidates)}


# Interrupted atomic writes: ``x.tmp``, ``.x.<task>.part`` and ``.x.<task>.part.mp4``,
# never numbered backup chunks such as ``x.vtbackup.part0001``.
_LEFTOVER = re.compile(r"(\.tmp|\.part(\.[A-Za-z][A-Za-z0-9]*)?)$")


def cleanup_temporary_files(root: str | Path, older_than_hours: int = 24, dry_run: bool = True) -> list[str]:
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_hours * 3600
    removed = []
    for path in Path(root).rglob("*"):
        if not _LEFTOVER.search(path.name) or not path.is_file():
            continue
        try:
            modified = path.stat().st_mtime
        except FileNotFoundError:
            continue  # an atomic write renamed it meanwhile: not a leftover
        if modified < cutoff:
            removed.append(str(path))
            if not dry_run:
                path.unlink(missing_ok=True)
    return removed
