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
        self.lock = threading.Lock()
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


def approve_video(store: JobStore, job: Job, actor: str = "api", *, expected_version: int | None = None) -> Job:
    """Approve assembled video and transition to READY.

    Approval is bound to the current ``active_video_version``.  An optional
    ``expected_version`` allows callers (Telegram callbacks) to reject a stale
    approval if the video has been regenerated since the preview was sent.
    """
    if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
        raise ValueError(f"Cannot approve video in status {job.status.value}")
    active = job.active_video_version
    if active is None:
        raise ValueError("No active video version to approve")
    if expected_version is not None and expected_version != active:
        raise ValueError(f"Stale video approval: expected v{expected_version}, active is v{active}")
    # Persist approval on the immutable version record
    vv = next((v for v in job.video_versions if v.version == active), None)
    if vv:
        vv.approved = True
        vv.approved_by = actor
        vv.approved_at = utc_now()
    store.transition(job, JobStatus.VIDEO_APPROVED, f"VIDEO APPROVED by {actor} (v{active})")
    store.transition(job, JobStatus.VIDEO_READY, f"VIDEO READY (v{active})")
    store.transition(job, JobStatus.READY, f"VIDEO APPROVED; JOB READY by {actor} (v{active})")
    _progress(job, "VIDEO_APPROVED")
    return job


def request_video_revision(store: JobStore, job: Job, revision: str, actor: str = "api") -> Job:
    """Request video revision — store structured revision and transition."""
    if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
        raise ValueError(f"Cannot request video revision in status {job.status.value}")
    job.video_revisions.append(VideoRevision(
        version=job.active_video_version or 0,
        text=revision, actor=actor,
    ))
    store.transition(job, JobStatus.VIDEO_REVISION_REQUESTED,
                     f"VIDEO REVISION REQUESTED by {actor}: {revision}")
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


def finalize_job(store: JobStore, job: Job, images: Path | list[Path]) -> Job:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return job
    image_list = [images] if isinstance(images, Path) else images
    store.update(job, JobStatus.ASSETS_READY, f"{len(image_list)} IMAGE(S) READY")
    transition_stage(job, StageName.ASSEMBLY, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSEMBLY, "ASSEMBLY STARTED")
    _progress(job, "ASSEMBLY")
    scenes = (job.script or {}).get("scenes", [])
    durations = [float(scene.get("duration", 5)) for scene in scenes]
    audio_files = [path for path in (store.root / job.job_id / "audio").glob("*")
                   if path.suffix.lower() in {".wav", ".mp3", ".aac", ".m4a", ".ogg"}]
    voice = next((path for path in audio_files if path.stem.startswith("voice")), None)
    music = next((path for path in audio_files if path.stem.startswith("music")), None)
    subtitles = _write_subtitles(store, job)
    brand_path = Path(os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json"
    try:
        brand = json.loads(brand_path.read_text(encoding="utf-8")) if brand_path.exists() else {}
    except ValueError:
        brand = {}
    watermark_value = brand.get("metadata", {}).get("watermark")
    watermark = Path(watermark_value) if watermark_value else None
    # Determine versioned output path (immutable: never overwrite previous version)
    job_dir = store.root / job.job_id
    final_dir = job_dir / "final"
    existing = [int(v.version) for v in job.video_versions if v.version is not None]
    next_version = max(existing, default=0) + 1
    output = final_dir / f"video-v{next_version}.mp4"
    # Use VideoEngine for rendering (per AGENTS.md §4.1/§28, Issue #31).
    # VideoEngine wraps AssemblyProvider for native engine, or dispatches to
    # remote engines (MoneyPrinter/ShortGPT) when configured.
    video_engine = providers.video_engine()
    video_engine.render(
        output,
        images=None if job.task_type == "video" else list(image_list),
        clips=list(image_list) if job.task_type == "video" else None,
        durations=durations,
        audio=voice,
        music=music,
        subtitles=subtitles,
        aspect_ratio=job.aspect_ratio,
        preset=job.output_preset,
        watermark=watermark,
        task_type=job.task_type,
    )
    if subtitles:
        register_artifact(job, store.root, subtitles, "subtitles", workflow="srt")
    register_artifact(job, store.root, output, "video", workflow="ffmpeg")
    # Record immutable video version
    rel_path = f"final/{output.name}"
    # Issue #49: bind the structured revision request to the version it produced,
    # so the render that follows a video revision documents (and does not silently
    # drop) the revision text it was generated for.
    revision_note = job.video_revisions[-1].text if job.video_revisions else None
    vv = VideoVersion(
        version=next_version, path=rel_path, sha256=_digest(output),
        revision_note=revision_note,
    )
    job.video_versions.append(vv)
    job.active_video_version = next_version
    job.output_path = str(output)
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
