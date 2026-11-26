"""Job CRUD, lifecycle and artifact routes for the Vertep CORE web application.

Extracted from ``core/app.py``: everything under ``/api/jobs`` plus the job
artifact download/upload/export/import endpoints and the legacy asset ``video``
and ``job_file`` endpoints.
"""
import asyncio
import hashlib
import io
import json
import os
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from adapters.providers import providers

from ..module.navigation import require_module
from ..artifacts import register_artifact, verify_artifacts, write_manifest
from ..configuration import (DefaultCharacterError, default_character_id, load_character,
                             missing_character_files)
from ..dispatcher import can_retry
from ..file_validation import validate_signature
from ..models import (JobCreate, JobStatus, JobUpdate, StageName, StageStatus,
                      StoryboardScene, StoryboardVersion, utc_now)
from ..orchestration import initialize_plan, transition_stage
from ..pipeline import approve_script, approve_video, claim_video_regeneration, generate_script, queue_storyboard, regenerate_script, regenerate_video, release_video_regeneration, request_script_revision, request_video_revision
from ..state import executor, store, task_queue
import secrets
import time
from ..pipeline import _active_video_file
from ..system_state import dispatch_allowed, get_system_state, jobs_may_be_created
from .job_helpers import (_job_action, _job_is_due,
                          _prepare_and_dispatch, _validate_workflow_reference)

router = APIRouter()


# Server-side equivalents of the Web UI status groups (see web-v2 presentation.ts).
STATUS_GROUPS: dict[str, set[str]] = {
    "active": {"SCRIPT_GENERATING", "STORYBOARD_GENERATING", "ASSET_GENERATION",
               "VIDEO_GENERATION", "ASSEMBLY", "PUBLISHING", "TTS_GENERATING"},
    "queued": {"NEW", "SCRIPT_QUEUED", "STORYBOARD_QUEUED"},
    "waiting": {"WAITING_FOR_SYSTEM", "PENDING_APPROVAL", "SCRIPT_PENDING_APPROVAL",
                "SCRIPT_REVISION_REQUESTED", "STORYBOARD_PENDING_APPROVAL",
                "STORYBOARD_READY", "STORYBOARD_REVISION_REQUESTED", "VIDEO_PENDING_APPROVAL",
                "VIDEO_REVISION_REQUESTED"},
    "completed": {"SCRIPT_READY", "SCRIPT_APPROVED", "STORYBOARD_APPROVED", "ASSETS_READY",
                  "VIDEO_READY", "VIDEO_APPROVED", "READY", "PUBLISHED"},
    "failed": {"FAILED", "SCRIPT_FAILED", "STORYBOARD_FAILED", "VIDEO_FAILED"},
}


@router.get("/api/public/videos/{job_id}/{version}/{expires}/{signature}.mp4", include_in_schema=False)
def public_video(job_id: str, version: int, expires: int, signature: str):
    """Serve one active video version to a platform that fetches it by address."""
    from .job_helpers import _public_video_signature
    expected = _public_video_signature(job_id, version, expires)
    job = store.jobs.get(job_id)
    if (not expected or not secrets.compare_digest(expected, signature) or expires < time.time()
            or job is None or job.active_video_version != version):
        raise HTTPException(404, "Video not found")
    path = _active_video_file(job, store.root)
    if not path.is_file():
        raise HTTPException(404, "Video not found")
    return FileResponse(path, media_type="video/mp4")


def _check_character_module(character_id: str, brand_id: str) -> str | None:
    """#499: with the Characters module READY it is the one library. A new job uses
    a character only if its owner brand runs it or the owner made it active for
    collaboration, with a complete profile. Existing/running jobs and pending
    approvals are not re-checked, so deactivation never stops them."""
    import httpx
    from urllib.parse import quote
    from core.module.gateway import call_module
    try:
        answer = call_module("characters", "GET", f"/characters/{quote(str(character_id), safe='')}/can-generate",
                             {"brand_id": brand_id})
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "Characters module is unavailable") from error
    if answer is None:
        return None  # the optional module is not in use: configuration characters only
    status, body = answer
    if status != 200 or not isinstance(body, dict):
        raise HTTPException(503, "Characters module is unavailable")
    if body.get("allowed") is not True:
        reasons = [str(item) for item in body.get("reasons") or []] or ["not allowed"]
        raise HTTPException(422, f"Character {character_id!r} cannot be used by brand {brand_id!r}: {'; '.join(reasons)}")
    owner = body.get("owner_brand_id")
    return owner if isinstance(owner, str) and owner else None


@router.post("/api/jobs")
def create_job(request: JobCreate, http_request: Request = None):
    from ..module.navigation import MODULE_INACTIVE, inactive_job_modules
    if inactive_job_modules():
        raise HTTPException(503, MODULE_INACTIVE)
    brands = getattr(getattr(http_request, "state", None), "vertep_brands", None)
    if brands is not None and request.brand_id not in brands:
        # A moderator creates jobs only for the brands of its account.
        raise HTTPException(403, "Brand access required")
    if not jobs_may_be_created():
        raise HTTPException(503, "Створення завдань тимчасово вимкнено поточним станом системи. Спробуйте пізніше.")
    if request.character_id is None:
        try:
            request.character_id = default_character_id()
        except DefaultCharacterError as error:
            raise HTTPException(422, str(error)) from error
    # i.0.0.4.85: a new job never starts from an incomplete character (§3.2), also
    # when it is named explicitly; reading existing jobs stays permissive.
    missing = missing_character_files(Path(os.getenv("CHARACTERS_ROOT", "characters")), request.character_id)
    if missing and (Path(os.getenv("CHARACTERS_ROOT", "characters")) / request.character_id).is_dir():
        raise HTTPException(422, f"Character {request.character_id!r} is incomplete: missing {', '.join(missing)}")
    character_owner = _check_character_module(request.character_id, request.brand_id)
    if request.workflow_template:
        from core.workflow.runtime import workflow_runtime
        runtime = workflow_runtime()
        if runtime is None:
            raise HTTPException(409, "Workflow templates are not configured")
        try:
            job_id = runtime.create(request.workflow_template, request.model_dump(exclude={"workflow_template"}))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"job_id": job_id, "workflow_template": request.workflow_template, **runtime.engine.get_job_status(job_id)}
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
                       request.aspect_ratio, request.output_preset, request.scheduled_for,
                       request.required_tags)
    if character_owner and character_owner != job.brand_id:
        # Material of another brand's character: publication needs that brand's approval.
        job.character_owner_brand = character_owner
        store.update(job, job.status, f"SHARED CONTENT: character owned by brand {character_owner}")
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
    # Issue #190: redact any publication metadata on return paths too.
    try:
        from .job_helpers import _redact_payload
        if getattr(job, "publication_results", None):
            job.publication_results = _redact_payload(job.publication_results)
    except Exception:
        pass
    return job


_scenario_job_lock = threading.Lock()


class ScenarioJobCreate(BaseModel):
    scenario_id: str = Field(min_length=2, max_length=64, pattern="^[a-z0-9][a-z0-9_-]{1,63}$")
    version: int = Field(ge=1)
    brand_id: str = Field(default="brand01", pattern="^[a-z0-9][a-z0-9_-]{1,63}$")
    character_id: str | None = Field(default=None, pattern="^[a-z0-9][a-z0-9_-]{1,63}$")
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    priority: int = Field(default=5, ge=1, le=10)
    task_type: str = "image"
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16)$")
    output_preset: str = "youtube"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


def _canonical_scenario_digest(scenario_id: str, version: int, title: str,
                               description: str, scenes: list[dict]) -> str:
    canonical = json.dumps({
        "scenario_id": scenario_id,
        "version": version,
        "title": title,
        "description": description,
        "scenes": scenes,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_job_from_scenario_snapshot(
    *,
    scenario_id: str,
    version: int,
    snapshot: dict,
    brand_id: str,
    allowed_brands: set[str] | None,
    actor: str,
    character_id: str | None = None,
    topic: str | None = None,
    priority: int = 5,
    task_type: str = "image",
    aspect_ratio: str = "16:9",
    output_preset: str = "youtube",
    idempotency_key: str | None = None,
):
    """#508: Create an independent CORE Job from a specific Storyboard scenario version.

    CORE checks target brand access, scenario access (owner or granted share), and
    every character referenced by the scenario; saves a self-contained initial
    storyboard snapshot with scenario provenance; and guarantees thread-safe
    idempotency on repeated requests with the same ``idempotency_key``.
    """
    from ..module.navigation import MODULE_INACTIVE, inactive_job_modules
    if inactive_job_modules():
        raise HTTPException(503, MODULE_INACTIVE)
    if allowed_brands is not None and brand_id not in allowed_brands:
        raise HTTPException(403, "Brand access required")
    if not jobs_may_be_created():
        raise HTTPException(503, "Створення завдань тимчасово вимкнено поточним станом системи. Спробуйте пізніше.")
    if not isinstance(snapshot, dict):
        raise HTTPException(422, "Invalid scenario version snapshot")
    if snapshot.get("archived"):
        raise HTTPException(409, "Cannot create a Job from an archived scenario")
    if str(snapshot.get("scenario_id") or "") != scenario_id or int(snapshot.get("version") or 0) != int(version):
        raise HTTPException(422, "Scenario snapshot does not match requested scenario_id/version")
    owner_brand = str(snapshot.get("owner_brand_id") or "")
    shared_brands = {str(item) for item in (snapshot.get("shared_brands") or [])}
    if not owner_brand or (brand_id != owner_brand and brand_id not in shared_brands):
        raise HTTPException(403, "Brand does not have access to this scenario")
    raw_scenes = snapshot.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise HTTPException(422, "Scenario version contains no scenes")
    snap_title = str(snapshot.get("title") or "").strip()
    if not snap_title:
        raise HTTPException(422, "Scenario version title is required")
    snap_description = str(snapshot.get("description") or "")
    computed_digest = _canonical_scenario_digest(scenario_id, int(version), snap_title, snap_description, raw_scenes)
    claimed_digest = snapshot.get("snapshot_sha256")
    if claimed_digest and claimed_digest != computed_digest:
        raise HTTPException(422, "Scenario snapshot digest mismatch")

    # Collect all characters referenced in the scenario scenes.
    scene_characters: list[str] = []
    for scene in raw_scenes:
        for cid in (scene.get("characters") or []) if isinstance(scene, dict) else []:
            if isinstance(cid, str) and cid and cid not in scene_characters:
                scene_characters.append(cid)
    # The IDs name directories under CHARACTERS_ROOT: the module supplies them, CORE checks them.
    from ..configuration import SAFE_ID
    for cid in [character_id, *scene_characters]:
        if cid is not None and (not isinstance(cid, str) or not SAFE_ID.fullmatch(cid)):
            raise HTTPException(422, "Invalid character ID")
    primary_char = character_id or (scene_characters[0] if scene_characters else None)
    if primary_char is None:
        try:
            primary_char = default_character_id()
        except DefaultCharacterError as error:
            raise HTTPException(422, str(error)) from error
    all_chars = [primary_char, *[c for c in scene_characters if c != primary_char]]
    characters_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    foreign_owners: list[str] = []
    for cid in all_chars:
        missing = missing_character_files(characters_root, cid)
        if missing and (characters_root / cid).is_dir():
            raise HTTPException(422, f"Character {cid!r} is incomplete: missing {', '.join(missing)}")
        owner = _check_character_module(cid, brand_id)
        if owner and owner != brand_id and owner not in foreign_owners:
            foreign_owners.append(owner)
    try:
        character = load_character(characters_root, primary_char)
    except Exception as error:
        raise HTTPException(400, f"Invalid character configuration: {error}") from error
    generation = character.generation
    workflow = generation.get("workflow") or character.workflow
    min_vram_mb = int(generation.get("min_vram_mb", 0))
    resolved_aspect = character.visual.get("aspect_ratio", aspect_ratio)
    resolved_preset = character.visual.get("output_preset", output_preset)
    try:
        workflow = _validate_workflow_reference(workflow, task_type)
    except (ValueError, OSError) as error:
        raise HTTPException(400, f"Invalid workflow: {error}") from error

    clean_key = (idempotency_key or "").strip() or None
    if clean_key is not None and len(clean_key) > 128:
        raise HTTPException(422, "idempotency_key must not exceed 128 characters")

    # Independent deep copy of scenes for the new Job.
    sb_scenes: list[StoryboardScene] = []
    script_scenes: list[dict] = []
    for idx, scene in enumerate(raw_scenes, 1):
        prompt = str(scene.get("prompt") or snap_title).strip()
        video_prompt = str(scene.get("video_prompt") or prompt).strip()
        voiceover = str(scene.get("voiceover") or scene.get("dialogue") or "").strip()
        duration = float(scene.get("duration", 5.0))
        scene_id = str(scene.get("scene_id") or f"scene-{idx:03d}")
        sb_scenes.append(StoryboardScene(
            index=idx,
            prompt=prompt,
            video_prompt=video_prompt,
            voiceover=voiceover,
            duration=duration,
            scene_id=scene_id,
        ))
        script_scenes.append({
            "scene_id": scene_id,
            "prompt": prompt,
            "video_prompt": video_prompt,
            "voiceover": voiceover,
            "dialogue": str(scene.get("dialogue") or voiceover),
            "location": str(scene.get("location") or ""),
            "characters": list(scene.get("characters") or []),
            "duration": duration,
        })

    resolved_topic = (topic or snap_title).strip()[:500]
    with _scenario_job_lock:
        if clean_key:
            for existing in store.jobs.values():
                src = existing.scenario_source or {}
                if (existing.scenario_idempotency_key == clean_key
                        and existing.brand_id == brand_id
                        and src.get("scenario_id") == scenario_id
                        and src.get("version") == int(version)):
                    return existing
        job = store.create(
            resolved_topic, primary_char, int(priority), f"scenario:{scenario_id}:v{version}",
            task_type, min_vram_mb, brand_id, workflow, resolved_aspect, resolved_preset,
        )
        sb_version = StoryboardVersion(
            version=1,
            title=snap_title,
            description=snap_description,
            scenes=sb_scenes,
            status="pending_approval",
            image_status="pending",
            approved_script={"title": snap_title, "scenes": script_scenes},
        )
        job.script = {"title": snap_title, "description": snap_description, "scenes": script_scenes}
        job.storyboards = [sb_version]
        job.active_storyboard_version = 1
        job.scenario_source = {
            "scenario_id": scenario_id,
            "version": int(version),
            "owner_brand_id": owner_brand,
            "title": snap_title,
            "snapshot_sha256": computed_digest,
            "imported_at": utc_now(),
        }
        job.scenario_idempotency_key = clean_key
        if foreign_owners:
            job.character_owner_brand = foreign_owners[0]
            job.character_owner_brands = list(foreign_owners)
        initialize_plan(job)
        target_status = JobStatus.STORYBOARD_PENDING_APPROVAL if dispatch_allowed() else JobStatus.WAITING_FOR_SYSTEM
        store.update(
            job,
            target_status,
            f"SCENARIO SNAPSHOT IMPORTED: {scenario_id} v{version} by {actor}",
        )
        if target_status == JobStatus.STORYBOARD_PENDING_APPROVAL:
            from ..image_storyboard import queue_image_storyboard
            queue_image_storyboard(store, job, 1)
        return job


@router.post("/api/jobs/from-scenario")
def create_job_from_scenario(request: ScenarioJobCreate, http_request: Request = None):
    """#508: Create a CORE Job from a saved Storyboard Scenario Library version."""
    import httpx
    from urllib.parse import quote
    from core.module.gateway import call_module

    identity = getattr(getattr(http_request, "state", None), "vertep_identity", None)
    actor = identity[0] if identity else "api"
    role = identity[1] if identity else "admin"
    brands = getattr(getattr(http_request, "state", None), "vertep_brands", None)
    if brands is not None and request.brand_id not in brands:
        raise HTTPException(403, "Brand access required")
    extra_headers = {
        "x-vertep-user": actor,
        "x-vertep-role": role,
        "x-vertep-brands": "*" if brands is None else ",".join(sorted(brands)),
    }
    try:
        answer = call_module(
            "storyboard",
            "GET",
            f"/scenarios/{quote(request.scenario_id, safe='')}/versions/{request.version}",
            params={"brand_id": request.brand_id},
            extra_headers=extra_headers,
        )
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "Storyboard module is unavailable") from error
    if answer is None:
        raise HTTPException(503, "Storyboard module is unavailable")
    status_code, snapshot = answer
    if status_code == 404:
        raise HTTPException(404, "Scenario version not found")
    if status_code == 403:
        raise HTTPException(403, "Scenario access denied")
    if status_code != 200 or not isinstance(snapshot, dict):
        raise HTTPException(503, "Storyboard module returned an unexpected response")
    header_key = http_request.headers.get("idempotency-key") if http_request else None
    key = request.idempotency_key or header_key
    return create_job_from_scenario_snapshot(
        scenario_id=request.scenario_id,
        version=request.version,
        snapshot=snapshot,
        brand_id=request.brand_id,
        allowed_brands=brands,
        actor=actor,
        character_id=request.character_id,
        topic=request.topic,
        priority=request.priority,
        task_type=request.task_type,
        aspect_ratio=request.aspect_ratio,
        output_preset=request.output_preset,
        idempotency_key=key,
    )



@router.get("/api/jobs")
def list_jobs(status: str | None = Query(default=None),
              status_group: str | None = Query(default=None),
              search: str | None = Query(default=None),
              page: int | None = Query(default=None, ge=1),
              per_page: int | None = Query(default=None, ge=1, le=200)):
    """List jobs with optional server-side filters and pagination.

    Filters mirror the Web UI local groups (``active``/``queued``/``waiting``/
    ``completed``/``failed``) plus an exact-status filter and a case-insensitive
    search over topic and job id. When ``page`` is supplied a JSON envelope is
    returned: ``{items, total, page, per_page, pages, has_more}``. Without any
    query parameters the legacy plain-array contract is preserved so existing
    consumers (SSE feed, scheduler polling, legacy UI) are unaffected.
    """
    jobs = list(store.jobs.values())
    if status:
        jobs = [job for job in jobs if job.status.value == status or job.status.name == status]
    if status_group:
        allowed = STATUS_GROUPS.get(status_group)
        if allowed is not None:
            jobs = [job for job in jobs if job.status.value in allowed]
    if search:
        needle = search.lower()
        jobs = [job for job in jobs if needle in job.topic.lower() or needle in job.job_id.lower()]
    if page is None:
        return jobs
    per_page = per_page or 20
    total = len(jobs)
    ordered = sorted(jobs, key=lambda job: job.created_at, reverse=True)
    start = (page - 1) * per_page
    items = ordered[start:start + per_page]
    pages = (total + per_page - 1) // per_page if total else 0
    return {"items": items, "total": total, "page": page, "per_page": per_page,
            "pages": pages, "has_more": page < pages}


@router.get("/api/jobs/{job_id}/events")
def job_structured_events(job_id: str):
    """Return the structured timeline for a job (real backend events, no parsing)."""
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return [event.model_dump(mode="json") for event in job.event_log]


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in store.jobs:
        raise HTTPException(404, "Job not found")
    job = store.jobs[job_id]
    # Issue #190: publication receipts/metadata served by CORE/UI may contain
    # secrets injected by a publisher/transport; redact before serialization.
    try:
        from .job_helpers import _redact_payload
        if getattr(job, "publication_results", None):
            job.publication_results = _redact_payload(job.publication_results)
    except Exception:
        pass
    return job




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
    if "character_id" in changes and changes["character_id"] != job.character_id:
        # A changed character is checked like a new job's; its owner joins the
        # brands whose approval the material needs (#499/#500).
        owner = _check_character_module(changes["character_id"], job.brand_id)
        if owner and owner != job.brand_id and owner not in job.character_owner_brands:
            job.character_owner_brands.append(owner)
            if job.character_owner_brand and job.character_owner_brand not in job.character_owner_brands:
                job.character_owner_brands.insert(0, job.character_owner_brand)
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
    from ..models import JobStatus
    mid_render = job.video_regenerating or (
        bool(getattr(job, 'assembly_task_ids', {})) and
        job.status in {JobStatus.ASSEMBLY, JobStatus.VIDEO_GENERATION}
    ) or job.status == JobStatus.PUBLISHING
    if mid_render:
        raise HTTPException(409, "Cannot regenerate while render in progress")
    if job.knowledge_snapshot:
        from ..knowledge_context import (
            KnowledgeRightsBlocked,
            KnowledgeVerificationUnavailable,
            verify_job_rerun_rights,
        )
        try:
            verify_job_rerun_rights(job)
        except KnowledgeVerificationUnavailable as error:
            raise HTTPException(503, "Character knowledge rights verification is unavailable") from error
        except KnowledgeRightsBlocked as error:
            raise HTTPException(409, "Character knowledge rights were revoked for rerun") from error
        job.knowledge_snapshot = None
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
        else:
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
    if getattr(job, 'assembly_task_ids', None):
        job.assembly_task_ids.clear()
    if getattr(job, 'assembly_cancel_requested', False):
        job.assembly_cancel_requested = False
    if getattr(job, 'video_regenerating', False):
        job.video_regenerating = False
    if getattr(job, 'video_versions', None):
        for vv in list(job.video_versions):
            path = vv.path if hasattr(vv, 'path') else None
            if path:
                candidate = (job_root / path).resolve()
                if not candidate.is_file():
                    try:
                        job.video_versions.remove(vv)
                    except ValueError:
                        pass
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
    from ..models import JobStatus
    mid_render = job.video_regenerating or (
        bool(getattr(job, 'assembly_task_ids', {})) and
        job.status in {JobStatus.ASSEMBLY, JobStatus.VIDEO_GENERATION}
    ) or job.status == JobStatus.PUBLISHING
    if mid_render:
        raise HTTPException(409, "Cannot retry while render in progress")
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
    # Issue #122 P5/P6: cancellation never reaches into an engine from CORE. The
    # in-flight attempt is fenced and the owning Worker is asked to stop the
    # upstream work; a late result is discarded, never imported.
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
    expected_video_version: int | None = Field(default=None, ge=1)


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
    from ..knowledge_context import KnowledgeRightsBlocked, KnowledgeVerificationUnavailable
    try:
        job = regenerate_script(store, job, body.revision)
    except KnowledgeVerificationUnavailable as error:
        raise HTTPException(503, "Character knowledge rights verification is unavailable") from error
    except KnowledgeRightsBlocked as error:
        raise HTTPException(409, "Character knowledge rights were revoked for rerun") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    executor.submit(_prepare_and_dispatch, job)
    return job



class VideoApproveAction(BaseModel):
    """Issue #81 R3: Web approval must name the reviewed video version.

    ``expected_video_version`` is mandatory so a stale browser tab can never
    approve a version the reviewer did not see; ``expected_sha256`` additionally
    binds the approval to the exact reviewed artifact.
    """
    actor: str = Field(default="api", min_length=1, max_length=200)
    expected_video_version: int = Field(ge=1)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


@router.post("/api/jobs/{job_id}/video/approve")
def approve_video_endpoint(job_id: str, body: VideoApproveAction):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        job = approve_video(store, job, body.actor,
                            expected_version=body.expected_video_version,
                            expected_sha256=body.expected_sha256)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    from ..auto_publish import publish_automatically
    publish_automatically(job.job_id)  # #502 AC3: channels with the automatic policy
    return store.jobs.get(job.job_id, job)


@router.post("/api/jobs/{job_id}/video/revision")
def revise_video(job_id: str, body: ScriptRevision):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        job = request_video_revision(store, job, body.revision or "", body.actor)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return job



@router.post("/api/jobs/{job_id}/video/regenerate")
def regenerate_video_endpoint(job_id: str, body: ScriptAction | None = None):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    actor = (body.actor if body else None) or "api"
    try:
        if job.status == JobStatus.VIDEO_PENDING_APPROVAL:
            job = request_video_revision(store, job, "regenerate", actor)
        elif job.status != JobStatus.VIDEO_REVISION_REQUESTED:
            raise ValueError(
                f"Cannot regenerate video in status {job.status.value}")
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    # Issue #82: the regeneration attempt is claimed atomically before dispatch
    # and released by the shared regeneration function on every exit path, so a
    # duplicate request cannot start a parallel render and a finished render
    # cannot leave the Job blocked.
    try:
        job = claim_video_regeneration(store, job, actor)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    # Submit real regeneration (shared path with Telegram)
    from core.pipeline import _finalize_video_regenerate
    try:
        executor.submit(_finalize_video_regenerate, job)
    except Exception as error:
        release_video_regeneration(store, job)
        raise HTTPException(500, f"Could not dispatch regeneration: {error}") from error
    return job



@router.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not store.delete(job_id):
        raise HTTPException(404, "Job not found")
    for task in task_queue.ready_tasks() + task_queue.inflight_tasks():
        if task.get("job_id") == job_id:
            task_queue.discard(task["task_id"])
    return {"deleted": job_id}


def _require_brand(http_request: Request | None, brand: str) -> str:
    """Administrator, or a moderator of ``brand``; returns the actor."""
    identity = getattr(getattr(http_request, "state", None), "vertep_identity", None)
    brands = getattr(getattr(http_request, "state", None), "vertep_brands", None)
    if brands is not None and brand not in brands:
        raise HTTPException(403, "Brand access required")
    return identity[0] if identity else "api"


@router.get("/api/shared-content")
def shared_content(brand_id: str, http_request: Request = None):
    """Material of other brands that uses this brand's characters (Brands tab)."""
    from ..shared_content import state
    _require_brand(http_request, brand_id)
    return {"brand_id": brand_id, "items": [state(job) for job in store.jobs.values()
                                             if job.character_owner_brand == brand_id and job.brand_id != brand_id]}


@router.post("/api/jobs/{job_id}/shared-approval")
def shared_approval(job_id: str, body: dict, http_request: Request = None):
    from ..shared_content import SharedContentError, decide, state
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    owner = str(body.get("owner_brand", ""))
    actor = _require_brand(http_request, owner)
    try:
        version = int(body.get("version"))
    except (TypeError, ValueError):
        raise HTTPException(422, "version is required") from None
    with store.lock:
        try:
            record = decide(job, owner, str(body.get("decision", "")), actor, version=version,
                            sha256=str(body.get("expected_sha256", "")))
        except SharedContentError as error:
            raise HTTPException(409, str(error)) from error
        store.update(job, job.status, f"SHARED CONTENT {record['decision'].upper()} v{version} BY {owner} ({actor})")
    if record["decision"] == "approve":
        from ..auto_publish import publish_automatically
        publish_automatically(job.job_id)  # the last owner's approval makes it publishable
    return state(job)


@router.post("/api/jobs/{job_id}/shared-approval/revoke")
def shared_approval_revoke(job_id: str, body: dict, http_request: Request = None):
    from ..shared_content import SharedContentError, revoke, state
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    owner = str(body.get("owner_brand", ""))
    actor = _require_brand(http_request, owner)
    with store.lock:
        try:
            revoke(job, owner, actor)
        except SharedContentError as error:
            raise HTTPException(409, str(error)) from error
        store.update(job, job.status, f"SHARED CONTENT APPROVAL REVOKED BY {owner} ({actor})")
    return state(job)


@router.post("/api/jobs/{job_id}/shared-publications")
def shared_publication(job_id: str, body: dict, http_request: Request = None):
    """A foreign owner brand publishes the approved material on its own channels."""
    from ..shared_content import SharedContentError, create_owner_publication
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    brand = str(body.get("brand_id", ""))
    actor = _require_brand(http_request, brand)
    with store.lock:
        try:
            derived = create_owner_publication(store, job, brand, actor, body)
        except SharedContentError as error:
            raise HTTPException(409, str(error)) from error
    return derived


@router.post("/api/jobs/{job_id}/publish", dependencies=[require_module("publishing")])
def publish_job(job_id: str, channels: list[str] | None = None):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    retryable_publish_failure = (job.status == JobStatus.FAILED and bool(job.publication_results)
                                 and bool(job.output_path) and Path(job.output_path).is_file())
    if job.status in {JobStatus.VIDEO_PENDING_APPROVAL, JobStatus.VIDEO_REVISION_REQUESTED, JobStatus.VIDEO_APPROVED}:
        raise HTTPException(409, f"Job is awaiting video approval (status: {job.status.value})")
    # Persisted approval of the active video version is a publish condition
    if job.script is not None and job.active_video_version is not None:
        active = next((v for v in job.video_versions if v.version == job.active_video_version), None)
        if active is None or not active.approved:
            raise HTTPException(409, "Job's active video version is not approved for publication")
    if job.status != JobStatus.READY and not retryable_publish_failure:
        raise HTTPException(409, "Job is not ready")
    from ..shared_content import publication_allowed
    with store.lock:
        allowed, reason = publication_allowed(job, store.root)
    if not allowed:
        raise HTTPException(409, reason)
    if job.knowledge_snapshot:
        from ..knowledge_context import (
            KnowledgeRightsBlocked,
            KnowledgeVerificationUnavailable,
            verify_job_publish_rights,
        )
        try:
            verify_job_publish_rights(job)
        except KnowledgeVerificationUnavailable as error:
            raise HTTPException(503, "Character knowledge rights verification is unavailable") from error
        except KnowledgeRightsBlocked as error:
            raise HTTPException(409, "Publication is blocked by character knowledge legal or critical hold") from error
    targets = channels or ["youtube"]
    from ..channel_secrets import mock_publishing, mode
    from ..publishing_targets import brand_channel
    if not mock_publishing():
        blocked = [channel for channel in targets if mode(brand_channel(job.brand_id, channel)) == "missing"]
        if blocked:
            raise HTTPException(409, f"Channel credentials are not configured for: {', '.join(blocked)}")
    publisher = providers.publisher()
    unknown = [channel for channel in targets if channel not in publisher.available_channels()]
    if unknown:
        raise HTTPException(400, f"Unknown publishers: {', '.join(unknown)}")
    from .job_helpers import _enqueue_publish_task, _has_publisher_worker, _publish_local
    has_publisher = _has_publisher_worker(store)
    transition_stage(job, StageName.PUBLISH, StageStatus.RUNNING)
    job.publish_attempt = 0
    job.publish_error = None
    from ..local_fallback import local_fallback_allowed
    local_fallback = local_fallback_allowed()
    if not has_publisher and local_fallback:
        results = {}
        for channel in targets:
            results[channel] = _publish_local(store, job, channel)
            receipt = job.publication_results.get(channel, results[channel])
            if receipt.get("status") == "PUBLISHED":
                job.published_to.append(channel)
        all_published = all(results[ch].get("status") == "PUBLISHED" for ch in targets)
        if all_published:
            transition_stage(job, StageName.PUBLISH, StageStatus.READY)
            return store.update(job, JobStatus.PUBLISHED, "PUBLISHED")
        not_configured = any(results[ch].get("status") == "NOT_CONFIGURED" for ch in targets)
        if not_configured:
            transition_stage(job, StageName.PUBLISH, StageStatus.FAILED, "NOT_CONFIGURED")
            return store.update(job, JobStatus.FAILED, "PUBLISHING FAILED OR NOT CONFIGURED")
        error = "; ".join(f"{ch}: {results[ch].get('error', results[ch].get('status'))}" for ch in targets)
        transition_stage(job, StageName.PUBLISH, StageStatus.FAILED, error)
        job.publish_error = error
        return store.update(job, JobStatus.FAILED, "PUBLISHING FAILED")
    queued_tasks = []
    for channel in targets:
        queued = _enqueue_publish_task(store, job, channel)
        if queued is not None:
            queued_tasks.append((channel, queued["task_id"]))
    if not queued_tasks:
        return store.update(job, JobStatus.PUBLISHED, "PUBLISHED (no channels)")
    store.update(job, JobStatus.PUBLISHING, f"PUBLISHING {len(queued_tasks)} channel(s) via Publisher Worker")
    return job



@router.get("/api/jobs/{job_id}/videos/{version}")
def video_version(job_id: str, version: int, sha256: str | None = None):
    """Exactly one immutable video version; with ``sha256`` it must be that file (#500)."""
    from ..pipeline import _digest
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    record = next((item for item in job.video_versions if item.version == version), None)
    if record is None:
        raise HTTPException(404, "Video not ready")
    path = store.root / job_id / record.path
    if (sha256 is not None and sha256 != record.sha256) or not path.is_file() or _digest(path) != record.sha256:
        raise HTTPException(409, "Artifact failed its integrity check")
    return FileResponse(path, media_type="video/mp4")


@router.get("/api/jobs/{job_id}/final/video.mp4")
def video(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.active_video_version is not None and any(v.version == job.active_video_version for v in job.video_versions):
        # The active version, never just the first video artifact in the list.
        return video_version(job_id, job.active_video_version)
    artifact = next((item for item in job.artifacts
                      if item.kind == "video" and
                      (item.path == "final/video.mp4" or
                       item.path.startswith("final/video-v"))), None)
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
    def save() -> object:
        destination.write_bytes(data)
        return register_artifact(job, store.root, destination, "input", workflow="user-upload")

    # i.0.0.4.77: writing and hashing the upload stays off the event loop.
    artifact = await asyncio.to_thread(save)
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
    character_id = original.get("character_id")
    if not character_id:
        try:
            character_id = default_character_id()
        except DefaultCharacterError as error:
            raise HTTPException(422, f"Imported project has no character: {error}") from error
        original["character_id"] = character_id
    imported = store.create(str(original.get("topic") or "Imported project"),
                            str(character_id),
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

    def extract() -> None:
        for item in archive.infolist():
            if item.is_dir() or item.filename == "job.json":
                continue
            destination = destination_root / item.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(item))
        write_manifest(imported, store.root)

    # i.0.0.4.77: unpacking and hashing the archive stays off the event loop.
    await asyncio.to_thread(extract)
    store.jobs[new_id] = imported
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
