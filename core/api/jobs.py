"""Job CRUD, lifecycle and artifact routes for the Vertep CORE web application.

Extracted from ``core/app.py``: everything under ``/api/jobs`` plus the job
artifact download/upload/export/import endpoints and the legacy asset ``video``
and ``job_file`` endpoints.
"""
import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from adapters.providers import providers

from ..artifacts import register_artifact, verify_artifacts, write_manifest
from ..configuration import load_character
from ..dispatcher import can_retry
from ..file_validation import validate_signature
from ..models import JobCreate, JobStatus, JobUpdate, StageName, StageStatus
from ..orchestration import initialize_plan, transition_stage
from ..pipeline import approve_script, generate_script, queue_storyboard, regenerate_script, request_script_revision
from ..state import executor, store, task_queue
from ..system_state import dispatch_allowed, get_system_state, jobs_may_be_created
from .job_helpers import (_job_action, _job_is_due,
                          _prepare_and_dispatch, _validate_workflow_reference)

router = APIRouter()


@router.post("/api/jobs")
def create_job(request: JobCreate):
    if not jobs_may_be_created():
        raise HTTPException(503, f"New jobs are disabled while system is {get_system_state()['state']}")
    try:
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), request.character_id)
    except Exception as error:
        raise HTTPException(400, f"Invalid character configuration: {error}") from error
    generation = character.generation
    if request.workflow is None:
        request.workflow = generation.get("workflow") or character.workflow
    if request.min_vram_mb == 0:
        request.min_vram_mb = int(generation.get("min_vram_mb", 0))
    request.aspect_ratio = character.visual.get("aspect_ratio", request.aspect_ratio)
    request.output_preset = character.visual.get("output_preset", request.output_preset)
    try:
        request.workflow = _validate_workflow_reference(request.workflow, request.task_type)
    except (ValueError, OSError) as error:
        raise HTTPException(400, f"Invalid workflow: {error}") from error
    job = store.create(request.topic, request.character_id, request.priority, request.source,
                       request.task_type, request.min_vram_mb, request.brand_id, request.workflow,
                       request.aspect_ratio, request.output_preset, request.scheduled_for)
    if not dispatch_allowed():
        store.update(job, JobStatus.WAITING_FOR_SYSTEM,
                     f"WAITING FOR SYSTEM: {get_system_state()['state']}")
    if request.scheduled_for:
        try:
            datetime.fromisoformat(request.scheduled_for.replace("Z", "+00:00"))
        except ValueError as error:
            store.delete(job.job_id)
            raise HTTPException(422, "scheduled_for must be an ISO-8601 datetime") from error
    if job.status == JobStatus.NEW and _job_is_due(job):
        executor.submit(_prepare_and_dispatch, job)
    elif job.status == JobStatus.NEW:
        store.event(job, f"SCHEDULED FOR {job.scheduled_for}")
    return job


@router.get("/api/jobs")
def list_jobs():
    return list(store.jobs.values())


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in store.jobs:
        raise HTTPException(404, "Job not found")
    return store.jobs[job_id]


@router.get("/api/jobs/{job_id}/assets")
def job_assets(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    integrity = {item["artifact_id"]: item["valid"] for item in verify_artifacts(job, store.root)}
    return [{"artifact_id": item.artifact_id, "name": item.filename, "kind": item.kind,
             "size": item.size, "mime_type": item.mime_type, "valid": integrity[item.artifact_id],
             "url": f"/api/jobs/{job_id}/artifacts/{item.artifact_id}/download"}
            for item in job.artifacts]


@router.patch("/api/jobs/{job_id}")
def edit_job(job_id: str, request: JobUpdate):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    changes = request.model_dump(exclude_none=True)
    expected_version = changes.pop("expected_version", None)
    if expected_version is not None and expected_version != job.version:
        raise HTTPException(409, f"Job changed; current version is {job.version}")
    prompt = changes.pop("prompt", None)
    script_changed = "script" in changes
    if "workflow" in changes:
        try:
            changes["workflow"] = _validate_workflow_reference(changes["workflow"], job.task_type)
        except (ValueError, OSError) as error:
            raise HTTPException(400, f"Invalid workflow: {error}") from error
    for key, value in changes.items():
        setattr(job, key, value)
    if prompt is not None:
        job.script = job.script or {"title": job.topic, "scenes": [{}]}
        scenes = job.script.setdefault("scenes", [{}])
        if not scenes:
            scenes.append({})
        scenes[0]["prompt"] = prompt
        script_changed = True
    if script_changed:
        job.scenes = []
        initialize_plan(job)
    job.version += 1
    if job.script is not None:
        (store.root / job.job_id / "script.json").write_text(json.dumps(job.script, indent=2), encoding="utf-8")
    return store.event(job, "JOB EDITED")


@router.post("/api/jobs/{job_id}/regenerate")
def regenerate_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    for task_id, scene_id in list(job.active_task_ids.items()):
        scene = next((item for item in job.scenes if item.scene_id == scene_id), None)
        if scene and scene.assigned_worker:
            task_queue.request_cancel(scene.assigned_worker, task_id)
        task_queue.discard(task_id)
    job_root = (store.root / job.job_id).resolve()
    retained_artifacts = []
    for artifact in job.artifacts:
        if artifact.kind == "input":
            retained_artifacts.append(artifact)
            continue
        path = (job_root / artifact.path).resolve()
        if job_root in path.parents and path.is_file():
            path.unlink()
    job.retries = 0
    job.script = None
    job.scenes = []
    job.stages = {}
    job.artifacts = retained_artifacts
    job.active_task_id = None
    job.active_task_ids.clear()
    job.completed_task_ids.clear()
    job.assigned_worker = None
    job.output_path = None
    job.approved = False
    job.published_to.clear()
    job.publication_results.clear()
    job.version += 1
    write_manifest(job, store.root)
    store.update(job, JobStatus.NEW, "REGENERATION REQUESTED")
    executor.submit(_prepare_and_dispatch, job)
    return job


@router.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not can_retry(job):
        return store.update(job, JobStatus.FAILED, "MAX RETRIES REACHED")
    job.retries += 1
    store.update(job, JobStatus.NEW, "RETRY REQUESTED")
    executor.submit(_prepare_and_dispatch, job)
    return job


@router.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    return _job_action(job_id, JobStatus.PAUSED, "JOB PAUSED")


@router.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    job = _job_action(job_id, JobStatus.NEW, "JOB RESUMED")
    executor.submit(_prepare_and_dispatch, job)
    return job


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    return _job_action(job_id, JobStatus.CANCELLED, "JOB CANCELLED")


@router.post("/api/jobs/{job_id}/approve")
def approve_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.approved = True
    return store.event(job, "JOB APPROVED")


class ScriptAction(BaseModel):
    actor: str = Field(default="api", min_length=1, max_length=200)


class ScriptRevision(ScriptAction):
    revision: str | None = Field(default=None, max_length=4000)


@router.post("/api/jobs/{job_id}/script/approve")
def approve_script_endpoint(job_id: str, body: ScriptAction):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        job = approve_script(store, job, body.actor)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    executor.submit(_prepare_and_dispatch, job)
    return job


@router.post("/api/jobs/{job_id}/script/revision")
def revise_script(job_id: str, body: ScriptRevision):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        job = request_script_revision(store, job, body.revision or "", body.actor)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return job


@router.post("/api/jobs/{job_id}/script/regenerate")
def regenerate_script_endpoint(job_id: str, body: ScriptRevision):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        job = regenerate_script(store, job, body.revision)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    executor.submit(_prepare_and_dispatch, job)
    return job


@router.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not store.delete(job_id):
        raise HTTPException(404, "Job not found")
    return {"deleted": job_id}


@router.post("/api/jobs/{job_id}/publish")
def publish_job(job_id: str, channels: list[str] | None = None):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    retryable_publish_failure = (job.status == JobStatus.FAILED and bool(job.publication_results)
                                 and bool(job.output_path) and Path(job.output_path).is_file())
    if job.status != JobStatus.READY and not retryable_publish_failure:
        raise HTTPException(409, "Job is not ready")
    targets = channels or ["youtube"]
    publisher = providers.publisher()
    unknown = [channel for channel in targets if channel not in publisher.available_channels()]
    if unknown:
        raise HTTPException(400, f"Unknown publishers: {', '.join(unknown)}")
    metadata = (job.script or {}) | {"job_id": job.job_id, "character_id": job.character_id,
                                     "brand_id": job.brand_id}
    results = {}
    attempts = max(1, int(os.getenv("PUBLISH_MAX_RETRIES", str(job.max_retries))))
    for attempt in range(1, attempts + 1):
        transition_stage(job, StageName.PUBLISH, StageStatus.RUNNING)
        store.update(job, JobStatus.PUBLISHING, f"PUBLISHING {attempt}/{attempts}: {', '.join(targets)}")
        results = {}
        for channel in targets:
            try:
                results[channel] = publisher.publish(channel, job.output_path or "", metadata)
            except Exception as error:
                from ..state import logger
                logger.exception("Publisher failed", extra={"job_id": job.job_id, "action": channel})
                results[channel] = {"channel": channel, "status": "FAILED", "error": str(error)}
        failed = [result for result in results.values() if result.get("status") != "PUBLISHED"]
        if not failed:
            transition_stage(job, StageName.PUBLISH, StageStatus.READY)
            break
        error = "; ".join(str(result.get("error") or result.get("status")) for result in failed)
        transition_stage(job, StageName.PUBLISH, StageStatus.FAILED, error)
        if any(result.get("status") == "NOT_CONFIGURED" for result in failed):
            break
        store.event(job, f"PUBLISH RETRY {attempt}/{attempts}: {error}")
    job.publication_results.update(results)
    job.published_to = [channel for channel, result in results.items() if result["status"] == "PUBLISHED"]
    if len(job.published_to) == len(targets):
        return store.update(job, JobStatus.PUBLISHED, "PUBLISHED")
    return store.update(job, JobStatus.FAILED, "PUBLISHING FAILED OR NOT CONFIGURED")


@router.get("/jobs/{job_id}/final/video.mp4")
def video(job_id: str):
    job = store.jobs.get(job_id)
    artifact = next((item for item in job.artifacts
                     if item.kind == "video" and item.path == "final/video.mp4"), None) if job else None
    if not artifact:
        raise HTTPException(404, "Video not ready")
    return download_artifact(job_id, artifact.artifact_id)


@router.get("/api/jobs/{job_id}/artifacts")
def job_artifacts(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "artifacts": job.artifacts}


@router.post("/api/jobs/{job_id}/artifacts/verify")
def verify_job_artifacts(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    results = verify_artifacts(job, store.root)
    return {"job_id": job_id, "valid": all(item["valid"] for item in results), "results": results}


@router.get("/api/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(job_id: str, artifact_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    artifact = next((item for item in job.artifacts if item.artifact_id == artifact_id), None)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    integrity = next(item for item in verify_artifacts(job, store.root) if item["artifact_id"] == artifact_id)
    if not integrity["valid"]:
        raise HTTPException(409, "Artifact failed its integrity check")
    path = store.root / job_id / artifact.path
    return FileResponse(path, media_type=artifact.mime_type, filename=artifact.filename)


@router.put("/api/jobs/{job_id}/uploads/{folder}/{filename}")
async def upload_job_file(job_id: str, folder: str, filename: str, request: Request):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    allowed = {
        "references": {".png", ".jpg", ".jpeg", ".webp", ".txt", ".json"},
        "audio": {".wav", ".mp3", ".aac", ".m4a", ".ogg"},
    }
    safe_name = Path(filename).name
    suffix = Path(safe_name).suffix.lower()
    if folder not in allowed or safe_name != filename or suffix not in allowed[folder]:
        raise HTTPException(400, "Unsupported upload target or file type")
    limit = int(os.getenv("MAX_UPLOAD_BYTES", "26214400"))
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Upload is too large")
        chunks.append(chunk)
    if not size:
        raise HTTPException(400, "Upload is empty")
    data = b"".join(chunks)
    try:
        validate_signature(data, suffix)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    destination = store.root / job_id / folder / safe_name
    if destination.exists():
        raise HTTPException(409, "File already exists")
    destination.write_bytes(data)
    artifact = register_artifact(job, store.root, destination, "input", workflow="user-upload")
    store.event(job, f"INPUT {artifact.artifact_id} UPLOADED")
    return artifact


@router.get("/api/jobs/{job_id}/export")
def export_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    source = store.root / job_id
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                archive.write(path, path.relative_to(source).as_posix())
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="vertep-{job_id}.zip"'})


@router.post("/api/projects/import")
async def import_project(request: Request):
    data = await request.body()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        if "job.json" not in names:
            raise ValueError("job.json is missing")
        if sum(item.file_size for item in archive.infolist()) > int(os.getenv("MAX_IMPORT_BYTES", "209715200")):
            raise ValueError("uncompressed archive is too large")
        for name in names:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts:
                raise ValueError("unsafe archive path")
        original = json.loads(archive.read("job.json"))
    except (ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise HTTPException(400, f"Invalid project archive: {error}") from error
    imported = store.create(str(original.get("topic") or "Imported project"),
                            str(original.get("character_id") or "did_samogon"),
                            int(original.get("priority", 5)), source="import")
    new_id = imported.job_id
    imported_status = str(original.get("status", JobStatus.PAUSED.value))
    if imported_status not in {JobStatus.READY.value, JobStatus.PUBLISHED.value,
                               JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
        imported_status = JobStatus.PAUSED.value
    original.update({"job_id": new_id, "created_at": imported.created_at,
                     "status": imported_status, "source": "import",
                     "active_task_id": None, "active_task_ids": {}, "assigned_worker": None,
                     "output_path": None})
    try:
        imported = type(imported).model_validate(original)
    except ValueError as error:
        store.delete(new_id)
        raise HTTPException(400, f"Invalid project metadata: {error}") from error
    destination_root = store.root / new_id
    for item in archive.infolist():
        if item.is_dir() or item.filename == "job.json":
            continue
        destination = destination_root / item.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(item))
    store.jobs[new_id] = imported
    write_manifest(imported, store.root)
    store.event(imported, "PROJECT IMPORTED AND PAUSED")
    return imported


@router.get("/jobs/{job_id}/files/{folder}/{filename}")
def job_file(job_id: str, folder: str, filename: str):
    if folder not in {"images", "audio", "subtitles", "final"}:
        raise HTTPException(400, "Unsupported asset folder")
    safe_name = Path(filename).name
    job = store.jobs.get(job_id)
    artifact = next((item for item in job.artifacts if item.path == f"{folder}/{safe_name}"), None) if job else None
    if not artifact:
        raise HTTPException(404, "Asset not found")
    return download_artifact(job_id, artifact.artifact_id)