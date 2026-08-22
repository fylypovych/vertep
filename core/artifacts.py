import hashlib
import json
import mimetypes
import uuid
import threading
from collections import defaultdict
from pathlib import Path

from .models import ArtifactRecord, Job, utc_now

_manifest_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def write_manifest(job: Job, job_root: Path) -> Path:
    with _manifest_locks[job.job_id]:
        path = job_root / job.job_id / "manifest.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"job_id": job.job_id, "version": job.version,
                                         "artifacts": [item.model_dump() for item in job.artifacts]},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return path


def register_artifact(job: Job, job_root: Path, path: Path, kind: str, *,
                      scene_id: str | None = None, task_id: str | None = None,
                      node_name: str | None = None, workflow: str | None = None) -> ArtifactRecord:
    with _manifest_locks[job.job_id]:
        path = path.resolve()
        base = (job_root / job.job_id).resolve()
        try:
            relative = path.relative_to(base)
        except ValueError as error:
            raise ValueError("Artifact must be inside the job directory") from error
        if not path.is_file():
            raise FileNotFoundError(path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        record = ArtifactRecord(
            artifact_id=f"art-{uuid.uuid4().hex}", kind=kind,
            path=relative.as_posix(), filename=path.name, mime_type=mime_type,
            size=path.stat().st_size, sha256=_digest(path), scene_id=scene_id,
            task_id=task_id, node_name=node_name, workflow=workflow,
            created_at=utc_now(),
        )
        job.artifacts.append(record)
        job.version += 1
        write_manifest(job, job_root)
        return record


def verify_artifacts(job: Job, job_root: Path) -> list[dict]:
    base = (job_root / job.job_id).resolve()
    results = []
    for item in job.artifacts:
        path = (base / item.path).resolve()
        safe = path == base or base in path.parents
        exists = safe and path.is_file()
        actual_sha256 = _digest(path) if exists else None
        results.append({"artifact_id": item.artifact_id, "path": item.path,
                        "exists": exists, "valid": exists and actual_sha256 == item.sha256,
                        "expected_sha256": item.sha256, "actual_sha256": actual_sha256})
    return results
