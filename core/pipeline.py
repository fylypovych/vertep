import json
import os
import shutil
import threading
from pathlib import Path
from .models import Job, JobEvent, JobStatus, utc_now, job_transition_allowed, VideoVersion, VideoRevision
from adapters.providers import providers
from adapters.telegram import TelegramAdapter
from .logging_config import configure_logging
from .repository import StateRepository, build_repository
from .orchestration import initialize_plan, recover_after_restart, transition_stage
from .models import StageName, StageStatus
from .artifacts import register_artifact, _digest
from .script_schema import normalize_script
from .dispatcher import available_worker

logger = configure_logging("core")


def _active_video_file(job: Job, job_root: Path) -> Path:
    """Resolve the current active video file path, with backward compat fallback.

    Prefers the versioned active output (``final/video-v<N>.mp4``); falls back to
    the legacy ``final/video.mp4`` when no versioned record exists yet.
    """
    if job.active_video_version is not None:
        vv = next((v for v in job.video_versions if v.version == job.active_video_version), None)
        if vv and vv.path:
            candidate = (job_root / job.job_id / vv.path).resolve()
            if candidate.is_file():
                return candidate
    legacy = (job_root / job.job_id / "final" / "video.mp4")
    return legacy

def _progress(job: Job, text: str) -> None:
    if not job.source.startswith("telegram:"):
        return
    chat_id = job.source.split(":", 2)[1]
    try:
        TelegramAdapter().send_message(chat_id, f"{job.job_id}: {text}")
    except Exception:
        logger.warning("Telegram progress notification failed", extra={"job_id": job.job_id})

class JobStore:
    def __init__(self, root: str = "jobs", repository: StateRepository | None = None, executor=None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.workers: dict[str, dict] = {}
        # Re-entrant: the status gate of approve/revision request must hold the
        # same lock that store.update() takes, otherwise two concurrent callers
        # can both pass the gate (Issue #81 R3 concurrency evidence).
        self.lock = threading.RLock()
        self.sequence = 0
        self.repository = repository or build_repository(root)
        self.executor = executor
        self._load()
        self.workers = {worker["node_name"]: worker for worker in self.repository.load_workers()}

    def load_workers(self, role: str | None = None, status: str | None = None, capability: str | None = None) -> list[dict]:
        return list(self.repository.load_workers(role=role, status=status, capability=capability))

    def _load(self) -> None:
        for loaded_job in self.repository.load_jobs():
            try:
                job = loaded_job
                original_status = job.status
                # Issue #82: a regeneration claim is a per-attempt lease held by an
                # in-process render thread. No such thread survives a CORE restart,
                # so a persisted claim is stale by definition and is released on
                # load; otherwise Telegram/Web would refuse every later attempt.
                if job.video_regenerating:
                    job.video_regenerating = False
                    job.events.append(f"{utc_now()} STALE VIDEO REGENERATION CLAIM RELEASED")
                    job.event_log.append(JobEvent(
                        message="STALE VIDEO REGENERATION CLAIM RELEASED",
                        type="status", state=original_status.value))
                    self.repository.save_job(job)
                if original_status == JobStatus.PUBLISHING:
                    job.status = JobStatus.READY
                    job.events.append(f"{utc_now()} PUBLISHING INTERRUPTED; RETURNED TO READY")
                    job.event_log.append(JobEvent(message="PUBLISHING INTERRUPTED; RETURNED TO READY",
                                                  type="status", state="READY"))
                    self.repository.save_job(job)
                elif original_status == JobStatus.VIDEO_READY and _active_video_file(job, self.root).is_file():
                    approved_version = next(
                        (vv for vv in job.video_versions if vv.approved), None
                    ) if job.video_versions else None
                    if approved_version and job.active_video_version and approved_version.version == job.active_video_version:
                        job.status = JobStatus.READY
                        job.events.append(f"{utc_now()} VIDEO RECOVERED AS READY (version {approved_version.version} approved)")
                        job.event_log.append(JobEvent(message=f"VIDEO RECOVERED AS READY (version {approved_version.version} approved)",
                                                      type="status", state="READY"))
                    else:
                        job.status = JobStatus.VIDEO_PENDING_APPROVAL
                        job.events.append(f"{utc_now()} VIDEO RECOVERED; REQUIRES APPROVAL")
                        job.event_log.append(JobEvent(message="VIDEO RECOVERED; REQUIRES APPROVAL",
                                                      type="status", state="VIDEO_PENDING_APPROVAL"))
                    self.repository.save_job(job)
                elif original_status in {JobStatus.SCRIPT_GENERATING, JobStatus.SCRIPT_READY,
                                          JobStatus.ASSET_GENERATION, JobStatus.ASSETS_READY,
                                          JobStatus.TTS_GENERATING, JobStatus.TTS_READY,
                                          JobStatus.VIDEO_GENERATION, JobStatus.VIDEO_READY,
                                          JobStatus.ASSEMBLY}:
                    recover_after_restart(job)
                    job.status = JobStatus.NEW
                    job.events.append(f"{utc_now()} RECOVERED AFTER RESTART")
                    job.event_log.append(JobEvent(message="RECOVERED AFTER RESTART",
                                                  type="status", state="NEW"))
                    self.repository.save_job(job)
                self.jobs[job.job_id] = job
                self.sequence = max(self.sequence, int(job.job_id.split("-")[-1]))
            except (ValueError, OSError):
                continue

    def create(self, topic: str, character_id: str, priority: int, source: str = "web",
               task_type: str = "image", min_vram_mb: int = 0, brand_id: str = "brand01",
               workflow: str | None = None, aspect_ratio: str = "16:9", output_preset: str = "youtube",
               scheduled_for: str | None = None, required_tags: list[str] | None = None) -> Job:
        with self.lock:
            year = int(utc_now()[:4])
            self.sequence = self.repository.next_job_sequence(year, self.sequence)
            job_id = f"{year}-{self.sequence:06d}"
            job = Job(job_id=job_id, topic=topic, character_id=character_id,
                      priority=priority, status=JobStatus.NEW,
                      created_at=utc_now(), source=source,
                      task_type=task_type, min_vram_mb=min_vram_mb,
                      brand_id=brand_id, workflow=workflow,
                      aspect_ratio=aspect_ratio,
                      output_preset=output_preset,
                      scheduled_for=scheduled_for,
                      required_tags=list(required_tags or []),
                      max_retries=int(os.getenv("MAX_RETRIES", "3")),
                      events=[f"{utc_now()} JOB CREATED"],
                      event_log=[JobEvent(message="JOB CREATED", type="create", state="NEW")])
            directory = self.root / job_id
            for name in ("references", "images", "video", "audio", "subtitles", "final"):
                (directory / name).mkdir(parents=True, exist_ok=True)
            self.jobs[job_id] = job
            (directory / "topic.txt").write_text(topic + "\n", encoding="utf-8")
            self._save(job)
            return job

    def delete(self, job_id: str) -> bool:
        with self.lock:
            if job_id not in self.jobs:
                return False
            del self.jobs[job_id]
            self.repository.delete_job(job_id)
            shutil.rmtree(self.root / job_id, ignore_errors=True)
            return True

    def event(self, job: Job, message: str, *, type: str = "info",
              state: str | None = None, attempt: int | None = None,
              node: str | None = None, task_id: str | None = None,
              artifact_id: str | None = None, error: str | None = None) -> Job:
        with self.lock:
            created_at = utc_now()
            job.events.append(f"{created_at} {message}")
            job.event_log.append(JobEvent(timestamp=created_at, message=message, type=type,
                                          state=state, attempt=attempt, node=node,
                                          task_id=task_id, artifact_id=artifact_id, error=error))
            self._save(job)
            self.repository.append_event(job.job_id, created_at, message)
            logger.info(message, extra={"job_id": job.job_id})
            return job

    def update(self, job: Job, status: JobStatus, event: str,
               *, attempt: int | None = None, node: str | None = None,
               task_id: str | None = None, artifact_id: str | None = None,
               error: str | None = None, state: str | None = None) -> Job:
        with self.lock:
            job.status = status
            created_at = utc_now()
            job.events.append(f"{created_at} {event}")
            job.event_log.append(JobEvent(timestamp=created_at, message=event, type="status",
                                          state=state or status.value, attempt=attempt,
                                          node=node, task_id=task_id,
                                          artifact_id=artifact_id, error=error))
            self._save(job)
            self.repository.append_event(job.job_id, created_at, event)
            logger.info(event, extra={"job_id": job.job_id})
            return job

    def transition(self, job: Job, status: JobStatus, event: str) -> Job:
        if not job_transition_allowed(job.status, status):
            raise ValueError(f"Invalid job transition: {job.status.value} -> {status.value}")
        return self.update(job, status, event)

    def _save(self, job: Job) -> None:
        self.repository.save_job(job)

    def save_worker(self, worker: dict) -> None:
        self.repository.save_worker(worker)

def _load_character_prompt(job: Job) -> tuple[str, dict | None]:
    character_root = Path(os.getenv("CHARACTERS_ROOT", "characters")) / job.character_id
    prompt_path = character_root / "system_prompt.txt"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    character = None
    try:
        from .configuration import load_character
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id)
        character = character.model_dump()
    except Exception:
        pass
    return system_prompt, character

def generate_script(store: JobStore, job: Job) -> Job:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return job
    initialize_plan(job)
    system_prompt, character = _load_character_prompt(job)
    if job.script is not None:
        job.script = normalize_script(job.script, job.topic)
        transition_stage(job, StageName.SCRIPT, StageStatus.RUNNING)
        store.event(job, "APPROVED STORYBOARD USED AS SCRIPT")
        store.transition(job, JobStatus.SCRIPT_PENDING_APPROVAL, "SCRIPT PENDING APPROVAL")
        if job.source.startswith("telegram:"):
            _send_script_approval_to_telegram(job)
        else:
            _progress(job, "SCRIPT_PENDING_APPROVAL")
        return job
    from .api.job_helpers import _enqueue_script_task
    revision = getattr(job, 'revision', None)
    queued = _enqueue_script_task(store, job, system_prompt, character, revision)
    if queued is not None:
        return job
    return job

def approve_script(store: JobStore, job: Job, actor: str = "api") -> Job:
    if job.status != JobStatus.SCRIPT_PENDING_APPROVAL and job.status != JobStatus.SCRIPT_REVISION_REQUESTED:
        raise ValueError(f"Cannot approve script in status {job.status.value}")
    job.approved = True
    job.approval_status = "approved"
    job.version += 1
    store.transition(job, JobStatus.SCRIPT_APPROVED, f"SCRIPT APPROVED by {actor}")
    _progress(job, "SCRIPT_APPROVED")
    return job

def request_script_revision(store: JobStore, job: Job, revision: str, actor: str = "api") -> Job:
    if job.status != JobStatus.SCRIPT_PENDING_APPROVAL:
        raise ValueError(f"Cannot request script revision in status {job.status.value}")
    job.approval_status = "revision_requested"
    job.version += 1
    job.revision = revision
    store.transition(job, JobStatus.SCRIPT_REVISION_REQUESTED, f"SCRIPT REVISION REQUESTED by {actor}: {revision}")
    _progress(job, "SCRIPT_REVISION_REQUESTED")
    return job

def regenerate_script(store: JobStore, job: Job, revision: str | None = None) -> Job:
    if job.status != JobStatus.SCRIPT_REVISION_REQUESTED:
        raise ValueError(f"Cannot regenerate script in status {job.status.value}")
    # Issue #64 T3: carry the revision request forward.  Without this the
    # regenerated script is produced without the feedback that triggered the
    # revision, so the worker re-reads a stale prompt and the loop repeats.
    job.revision = revision
    job.script = None
    job.scenes = []
    job.stages = {}
    job.artifacts = [a for a in job.artifacts if a.kind == "input"]
    job.active_task_id = None
    job.script_task_id = None
    job.script_attempt = 0
    job.script_error = None
    job.active_task_ids.clear()
    job.completed_task_ids.clear()
    job.assigned_worker = None
    job.output_path = None
    job.version += 1
    store.transition(job, JobStatus.SCRIPT_QUEUED, "SCRIPT REGENERATING")
    return generate_script(store, job)

def queue_storyboard(store: JobStore, job: Job, revision: str | None = None) -> Job:
    from .storyboard import StoryboardService
    job.storyboard_error = None
    store.transition(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    service = StoryboardService(store, executor=store.executor)
    service.queue(job, revision)
    return job

def _write_subtitles(store: JobStore, job: Job) -> Path | None:
    scenes = (job.script or {}).get("scenes", [])
    if not scenes:
        return None
    def stamp(seconds: float) -> str:
        millis = int(seconds * 1000)
        return f"{millis // 3600000:02d}:{millis // 60000 % 60:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"
    cursor = 0.0
    blocks = []
    for index, scene in enumerate(scenes, 1):
        duration = float(scene.get("duration", 5))
        text = scene.get("voiceover") or scene.get("text") or ""
        if text:
            blocks.append(f"{index}\n{stamp(cursor)} --> {stamp(cursor + duration)}\n{text}\n")
        cursor += duration
    if not blocks:
        return None
    path = store.root / job.job_id / "subtitles" / "video.srt"
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path

def _send_video_approval_to_telegram(job: Job) -> None:
    """Send video approval request to Telegram admin chats."""
    chat_id = job.source.split(":", 2)[1]
    from .storyboard_telegram import send_video_for_approval, video_approval_keyboard
    keyboard = video_approval_keyboard(job.job_id, job.active_video_version)
    try:
        send_video_for_approval(chat_id, job)
    except Exception:
        pass
    try:
        TelegramAdapter().send_message(
            chat_id,
            f"🎥 Відео {job.job_id} зібрано. Затвердити для публікації?",
            keyboard,
        )
    except Exception:
        logger.warning("Telegram video approval notification failed",
                       extra={"job_id": job.job_id})
    for admin_chat in _admin_chat_ids():
        try:
            send_video_for_approval(admin_chat, job)
            TelegramAdapter().send_message(
                admin_chat,
                f"🎥 Відео {job.job_id} потребує затвердження.",
                keyboard,
            )
        except Exception:
            pass


def _send_script_approval_to_telegram(job: Job) -> None:
    """Send script approval request to Telegram admin chats."""
    chat_id = job.source.split(":", 2)[1]
    from .storyboard_telegram import render_script, script_keyboard
    script = job.script or {}
    try:
        chunks = render_script(script, job.job_id)
        keyboard = script_keyboard(job.job_id)
        for index, chunk in enumerate(chunks):
            markup = keyboard if index == len(chunks) - 1 else None
            TelegramAdapter().send_message(chat_id, chunk, markup)
    except Exception:
        logger.warning("Telegram script approval notification failed",
                       extra={"job_id": job.job_id})
    for admin_chat in _admin_chat_ids():
        if admin_chat == chat_id:
            continue
        try:
            chunks = render_script(script, job.job_id)
            keyboard = script_keyboard(job.job_id)
            for index, chunk in enumerate(chunks):
                markup = keyboard if index == len(chunks) - 1 else None
                TelegramAdapter().send_message(admin_chat, chunk, markup)
        except Exception:
            pass


def approve_video(store: JobStore, job: Job, actor: str = "api", *, expected_version: int | None = None,
                  expected_sha256: str | None = None) -> Job:
    """Approve assembled video and transition to READY.

    Approval is bound to the current ``active_video_version``.  An optional
    ``expected_version`` allows callers (Telegram callbacks) to reject a stale
    approval if the video has been regenerated since the preview was sent.
    ``expected_sha256`` binds the approval to the exact artifact the reviewer
    saw: it must match the immutable version record and the file on disk.

    The gate and the state mutation run under the store lock, so concurrent
    approvals of the same Job admit exactly one winner (Issue #81 R3).
    """
    with store.lock:
        if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
            raise ValueError(f"Cannot approve video in status {job.status.value}")
        active = job.active_video_version
        if active is None:
            raise ValueError("No active video version to approve")
        if expected_version is not None and expected_version != active:
            raise ValueError(f"Stale video approval: expected v{expected_version}, active is v{active}")
        # Persist approval on the immutable version record
        vv = next((v for v in job.video_versions if v.version == active), None)
        if expected_sha256 is not None:
            if vv is None or vv.sha256 != expected_sha256:
                raise ValueError(
                    f"Stale video approval: reviewed hash does not match active v{active}")
            reviewed = store.root / job.job_id / vv.path
            if not reviewed.is_file():
                raise ValueError(f"Reviewed video artifact for v{active} is missing")
            if _digest(reviewed) != expected_sha256:
                raise ValueError(
                    f"Video artifact integrity mismatch for v{active}")
        if vv:
            vv.approved = True
            vv.approved_by = actor
            vv.approved_at = utc_now()
        job.approved = True
        job.approval_status = "approved"
        store.transition(job, JobStatus.VIDEO_APPROVED, f"VIDEO APPROVED by {actor} (v{active})")
        store.transition(job, JobStatus.VIDEO_READY, f"VIDEO READY (v{active})")
        store.transition(job, JobStatus.READY, f"VIDEO APPROVED; JOB READY by {actor} (v{active})")
    _progress(job, "VIDEO_APPROVED")
    return job


def request_video_revision(store: JobStore, job: Job, revision: str, actor: str = "api") -> Job:
    """Request video revision — store structured revision and apply it.

    Two supported outcomes (Issue #81 R1):

    * ``regenerate`` — pure re-render of the current inputs, the Job enters
      ``VIDEO_REVISION_REQUESTED`` and the caller dispatches regeneration.
    * free text — the revision has no render parameter to change, so it is
      applied to generation: the Job is routed through script regeneration and
      the same text is carried forward to the storyboard regeneration that
      follows script approval (``video_revision_upstream``).

    Empty/whitespace text is unsupported and rejected explicitly instead of
    being recorded as a revision that would never change the video.

    The gate and the state mutation run under the store lock, so concurrent
    requests admit exactly one routed revision (Issue #81 R3).
    """
    text = (revision or "").strip()
    if not text:
        raise ValueError("Video revision text is required")
    with store.lock:
        if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
            raise ValueError(f"Cannot request video revision in status {job.status.value}")
        job.video_revisions.append(VideoRevision(
            version=job.active_video_version or 0,
            text=text, actor=actor,
        ))
        if text == "regenerate":
            store.transition(job, JobStatus.VIDEO_REVISION_REQUESTED,
                             f"VIDEO REVISION REQUESTED by {actor}: regenerate")
            return job
        # Issue #81 R1: apply the revision to generation, not only to revision_note.
        job.video_revision_upstream = text
        job.approval_status = "revision_requested"
        store.transition(job, JobStatus.SCRIPT_REVISION_REQUESTED,
                         f"VIDEO REVISION ROUTED TO SCRIPT by {actor}: {text}")
    return regenerate_script(store, job, revision=text)


def claim_video_regeneration(store: JobStore, job: Job, actor: str = "api") -> Job:
    """Reserve the single regeneration attempt slot of a Job (Issue #82).

    The claim is taken *before* the render is submitted and is persisted, so a
    second concurrent request — Web, Telegram or retry — is refused instead of
    starting a parallel render that would race on the same version number. The
    claim is released by :func:`release_video_regeneration` and by CORE restart
    (an in-flight render thread never survives a restart, so a persisted claim
    is stale by definition).
    """
    with store.lock:
        if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
            raise ValueError(f"Cannot regenerate video in status {job.status.value}")
        if job.video_regenerating:
            raise ValueError("Video regeneration already in progress")
        job.video_regenerating = True
        store._save(job)
        store.event(job, f"VIDEO REGENERATION CLAIMED by {actor}")
    return job


def release_video_regeneration(store: JobStore, job: Job) -> Job:
    """Release the regeneration claim on every exit path, success included."""
    with store.lock:
        if not job.video_regenerating:
            return job
        job.video_regenerating = False
        store._save(job)
        store.event(job, "VIDEO REGENERATION CLAIM RELEASED")
    return job


def regenerate_video(store: JobStore, job: Job) -> Job:
    """Re-run assembly after video revision request."""
    if job.status != JobStatus.VIDEO_REVISION_REQUESTED:
        raise ValueError(f"Cannot regenerate video in status {job.status.value}")
    store.transition(job, JobStatus.ASSEMBLY, "VIDEO REGENERATING")
    return job


def _admin_chat_ids() -> list[str]:
    from .telegram_store import get_admin_chat_ids
    return get_admin_chat_ids()


def _do_publish_single(job: Job, channel: str) -> dict:
    """Direct (CORE-side) publication to a single channel — used by local fallback.

    Returns the publication receipt dict. This is the synchronous, non-task-queue
    path; when a Publisher Worker is available the task goes through the queue
    instead.
    """
    from adapters.providers import providers
    video_path = job.output_path or ""
    metadata = (job.script or {}) | {"job_id": job.job_id, "character_id": job.character_id,
                                     "brand_id": job.brand_id}
    publisher = providers.publisher()
    if channel not in publisher.available_channels():
        return {"channel": channel, "status": "FAILED", "error": f"Unknown channel: {channel}"}
    return publisher.publish(channel, video_path, metadata)


def assembly_plan(job_store: JobStore, job: Job, images: list[Path]) -> dict:
    """Resolve the approved inputs of one assembly attempt (Issue #122 §9.3).

    Everything the render needs is read from the approved Job state, so the plan
    is identical for the Native route and for a Worker-executed attempt: the same
    materials, voice, music, subtitles, watermark, aspect, preset and script.
    """
    image_list = [images] if isinstance(images, Path) else list(images)
    scenes = (job.script or {}).get("scenes", [])
    durations = [float(scene.get("duration", 5)) for scene in scenes]
    audio_files = [path for path in (job_store.root / job.job_id / "audio").glob("*")
                   if path.suffix.lower() in {".wav", ".mp3", ".aac", ".m4a", ".ogg"}]
    voice = next((path for path in audio_files if path.stem.startswith("voice")), None)
    music = next((path for path in audio_files if path.stem.startswith("music")), None)
    subtitles = _write_subtitles(job_store, job)
    brand_path = Path(os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json"
    try:
        brand = json.loads(brand_path.read_text(encoding="utf-8")) if brand_path.exists() else {}
    except ValueError:
        brand = {}
    watermark_value = brand.get("metadata", {}).get("watermark")
    existing = [int(v.version) for v in job.video_versions if v.version is not None]
    next_version = max(existing, default=0) + 1
    job_dir = job_store.root / job.job_id
    return {
        "materials": image_list,
        "audio": voice,
        "music": music,
        "subtitles": subtitles,
        "watermark": Path(watermark_value) if watermark_value else None,
        "durations": durations,
        "aspect_ratio": job.aspect_ratio,
        "preset": job.output_preset,
        "task_type": job.task_type,
        "script": approved_narration(job),
        "version": next_version,
        "output": job_dir / "final" / f"video-v{next_version}.mp4",
    }


def approved_narration(job: Job) -> str:
    """Canonical approved narration text (Issue #122 §9.11.1).

    The concatenation of the approved scene texts in scene order, separated by one
    newline, with no pause tags and no content the operator has not approved: the
    external engine may never generate its own script (LLM) or narration (TTS).
    """
    scenes = (job.script or {}).get("scenes", [])
    return "\n".join(
        str(scene.get("text") or "").strip() for scene in scenes
        if str(scene.get("text") or "").strip()
    )


def effective_engine_id(engine) -> str:
    """Identifier of the effective video engine, ``native`` when there is none.

    Only a real engine id routes an assembly to a Worker; anything else (including
    a stand-in used in tests) keeps the Native control-plane route, so CORE never
    dispatches an assembly it cannot identify (Issue #122 P5).
    """
    value = getattr(engine, "engine_id", None)
    return value if isinstance(value, str) and value else "native"


def _finish_video_version(job: Job, job_store: JobStore, output: Path, version: int) -> None:
    """Record an accepted render as the new current immutable video version.

    Shared by the Native route and the Worker-executed assembly result, so a
    version is registered identically no matter which engine produced it.
    """
    revision_note = job.video_revisions[-1].text if job.video_revisions else None
    job.video_versions.append(VideoVersion(
        version=version, path=f"final/{output.name}", sha256=_digest(output),
        revision_note=revision_note,
    ))
    job.active_video_version = version
    job.output_path = str(output)
    # Issue #81 R1: the upstream text (script + storyboard regeneration) has
    # been consumed by this render, so it must not leak into later storyboard
    # regenerations of the job.
    job.video_revision_upstream = None
    final_dir = output.parent
    # Backward-compat pointer so legacy consumers of final/video.mp4 keep working
    legacy_latest = final_dir / "video.mp4"
    try:
        if legacy_latest.exists() or legacy_latest.is_symlink():
            legacy_latest.unlink()
        legacy_latest.symlink_to(output.name)
    except OSError:
        # Windows without developer mode: fallback to copying the file
        try:
            shutil.copy2(str(output), str(legacy_latest))
        except OSError:
            pass


def finalize_job(store: JobStore, job: Job, images: Path | list[Path]) -> Job:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return job
    image_list = [images] if isinstance(images, Path) else images
    store.update(job, JobStatus.ASSETS_READY, f"{len(image_list)} IMAGE(S) READY")
    transition_stage(job, StageName.ASSEMBLY, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSEMBLY, "ASSEMBLY STARTED")
    _progress(job, "ASSEMBLY")
    plan = assembly_plan(store, job, image_list)
    subtitles = plan["subtitles"]
    output = plan["output"]
    next_version = plan["version"]
    job_dir = store.root / job.job_id
    final_dir = job_dir / "final"
    video_engine = providers.video_engine()
    if effective_engine_id(video_engine) != "native":
        # Issue #122 P5: an external engine is executed by a Worker, never inside
        # CORE. CORE only decides the attempt (immutable engine/config snapshot,
        # durable submit key) and dispatches it; there is no CORE-side fallback to
        # Native, so a missing or incapable executor is a refusal, not a
        # substitution (§9.12).
        from .api.job_helpers import (_assembly_submit_key, _assembly_input_digest,
                                      _assembly_task_for, _engine_snapshot,
                                      _enqueue_assembly_task)
        digest = _assembly_input_digest(
            job, materials=plan["materials"], audio=plan["audio"],
            music=plan["music"], subtitles=subtitles, watermark=plan["watermark"],
            script=plan["script"],
        )
        submit_key = _assembly_submit_key(job, version=next_version, input_digest=digest)
        snapshot = _engine_snapshot(job, version=next_version, submit_key=submit_key,
                                 engine=video_engine)
        task = _assembly_task_for(job, submit_key=submit_key, snapshot=snapshot, **plan)
        _enqueue_assembly_task(job, task)
        store.event(job, f"ASSEMBLY v{next_version} DISPATCHED TO WORKER "
                         f"(engine={snapshot['engine_id']})")
        return job
    # Use VideoEngine for rendering (per AGENTS.md §4.1/§28, Issue #31).
    # VideoEngine wraps AssemblyProvider for native engine, or dispatches to
    # remote engines (MoneyPrinter/ShortGPT) when configured.
    video_engine.render(
        output,
        images=None if job.task_type == "video" else plan["materials"],
        clips=plan["materials"] if job.task_type == "video" else None,
        durations=plan["durations"],
        audio=plan["audio"],
        music=plan["music"],
        subtitles=subtitles,
        aspect_ratio=plan["aspect_ratio"],
        preset=plan["preset"],
        watermark=plan["watermark"],
        task_type=plan["task_type"],
        script=plan["script"],
    )
    if subtitles:
        register_artifact(job, store.root, subtitles, "subtitles", workflow="srt")
    # Issue #82: the render can take minutes, so a pause/cancel accepted while it
    # runs must win over the result. Re-reading the persisted state under the lock
    # closes the window between the entry check and the version registration —
    # otherwise a cancelled Job would be resurrected as VIDEO_PENDING_APPROVAL.
    with store.lock:
        superseded = job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}
    if superseded:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            pass
        transition_stage(job, StageName.ASSEMBLY, StageStatus.CANCELLED,
                         "render superseded by pause/cancel")
        store.event(job, f"ASSEMBLY RESULT DISCARDED (v{next_version}): "
                         f"job is {job.status.value} — no version registered")
        release_video_regeneration(store, job)
        return job
    register_artifact(job, store.root, output, "video", workflow="ffmpeg")
    job.video_engine_snapshot = {
        "engine_id": "native",
        "bridge_schema_version": None,
        "video_version": next_version,
        "config_revision": None,
    }
    _finish_video_version(job, store, output, next_version)
    store.update(job, JobStatus.VIDEO_READY, f"VIDEO READY (v{next_version})")
    transition_stage(job, StageName.ASSEMBLY, StageStatus.READY)
    store.update(job, JobStatus.VIDEO_PENDING_APPROVAL, f"VIDEO PENDING APPROVAL (v{next_version})")
    if job.source.startswith("telegram:"):
        _send_video_approval_to_telegram(job)
    else:
        _progress(job, "VIDEO_PENDING_APPROVAL")
    return job


def prepare_job(store: JobStore, job: Job) -> Job:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return job
    if job.status == JobStatus.NEW:
        store.transition(job, JobStatus.SCRIPT_QUEUED, "SCRIPT QUEUED")
    if job.script is None and job.status in {JobStatus.SCRIPT_QUEUED, JobStatus.SCRIPT_FAILED}:
        generate_script(store, job)
    return job

def prepare_job_safe(store: JobStore, job: Job) -> Job:
    try:
        return prepare_job(store, job)
    except Exception as error:
        logger.exception("Pipeline failed", extra={"job_id": job.job_id})
        store.update(job, JobStatus.FAILED, f"PIPELINE FAILED: {error}")
        return job

def finalize_job_safe(store: JobStore, job: Job, image: Path | list[Path]) -> Job:
    try:
        return finalize_job(store, job, image)
    except Exception as error:
        logger.exception("Assembly failed", extra={"job_id": job.job_id})
        try:
            transition_stage(job, StageName.ASSEMBLY, StageStatus.FAILED, str(error))
        except ValueError:
            pass
        store.update(job, JobStatus.FAILED, f"ASSEMBLY FAILED: {error}")
        return job
