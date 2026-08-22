import json
import os
import shutil
import threading
from pathlib import Path
from .models import Job, JobStatus, utc_now
from adapters.ffmpeg import FFmpegAdapter
from adapters.llm import LLMAdapter
from adapters.tts import TTSAdapter
from adapters.telegram import TelegramAdapter
from .logging_config import configure_logging
from .repository import StateRepository, build_repository
from .orchestration import initialize_plan, recover_after_restart, transition_stage
from .models import StageName, StageStatus
from .artifacts import register_artifact
from .script_schema import normalize_script

logger = configure_logging("core")

def _progress(job: Job, text: str) -> None:
    if not job.source.startswith("telegram:"):
        return
    chat_id = job.source.split(":", 2)[1]
    try:
        TelegramAdapter().send_message(chat_id, f"{job.job_id}: {text}")
    except Exception:
        logger.warning("Telegram progress notification failed", extra={"job_id": job.job_id})

class JobStore:
    def __init__(self, root: str = "jobs", repository: StateRepository | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.workers: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.repository = repository or build_repository(root)
        self._load()
        self.workers = {worker["node_name"]: worker for worker in self.repository.load_workers()}

    def _load(self) -> None:
        for loaded_job in self.repository.load_jobs():
            try:
                job = loaded_job
                original_status = job.status
                if original_status == JobStatus.PUBLISHING:
                    job.status = JobStatus.READY
                    job.events.append(f"{utc_now()} PUBLISHING INTERRUPTED; RETURNED TO READY")
                    self.repository.save_job(job)
                elif original_status == JobStatus.VIDEO_READY and (self.root / job.job_id / "final" / "video.mp4").is_file():
                    job.status = JobStatus.READY
                    job.events.append(f"{utc_now()} VIDEO RECOVERED AS READY")
                    self.repository.save_job(job)
                elif original_status in {JobStatus.SCRIPTING, JobStatus.SCRIPT_READY,
                                          JobStatus.ASSET_GENERATION, JobStatus.ASSETS_READY,
                                          JobStatus.VIDEO_GENERATION, JobStatus.VIDEO_READY,
                                          JobStatus.ASSEMBLY}:
                    recover_after_restart(job)
                    job.status = JobStatus.NEW
                    job.events.append(f"{utc_now()} RECOVERED AFTER RESTART")
                    self.repository.save_job(job)
                self.jobs[job.job_id] = job
                self.sequence = max(self.sequence, int(job.job_id.split("-")[-1]))
            except (ValueError, OSError):
                continue

    def create(self, topic: str, character_id: str, priority: int, source: str = "web",
               task_type: str = "image", min_vram_mb: int = 0, brand_id: str = "brand01",
               workflow: str | None = None, aspect_ratio: str = "16:9", output_preset: str = "youtube",
               scheduled_for: str | None = None) -> Job:
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
                      max_retries=int(os.getenv("MAX_RETRIES", "3")),
                      events=[f"{utc_now()} JOB CREATED"])
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

    def event(self, job: Job, message: str) -> Job:
        with self.lock:
            created_at = utc_now()
            job.events.append(f"{created_at} {message}")
            self._save(job)
            self.repository.append_event(job.job_id, created_at, message)
            logger.info(message, extra={"job_id": job.job_id})
            return job

    def update(self, job: Job, status: JobStatus, event: str) -> Job:
        with self.lock:
            job.status = status
            created_at = utc_now()
            job.events.append(f"{created_at} {event}")
            self._save(job)
            self.repository.append_event(job.job_id, created_at, event)
            logger.info(event, extra={"job_id": job.job_id})
            return job

    def _save(self, job: Job) -> None:
        self.repository.save_job(job)

    def save_worker(self, worker: dict) -> None:
        self.repository.save_worker(worker)

def prepare_job(store: JobStore, job: Job) -> Job:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return job
    initialize_plan(job)
    character_root = Path(os.getenv("CHARACTERS_ROOT", "characters")) / job.character_id
    prompt_path = character_root / "system_prompt.txt"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    script_error = None
    for script_attempt in range(1, max(1, job.max_retries) + 1):
        transition_stage(job, StageName.SCRIPT, StageStatus.RUNNING)
        store.update(job, JobStatus.SCRIPTING, f"SCRIPT STARTED {script_attempt}/{job.max_retries}")
        try:
            job.script = normalize_script(LLMAdapter().generate_script(job.topic, system_prompt), job.topic)
            break
        except (ValueError, TypeError) as error:
            script_error = error
            transition_stage(job, StageName.SCRIPT, StageStatus.FAILED, str(error))
            store.event(job, f"SCRIPT FAILED {script_attempt}/{job.max_retries}: {error}")
    else:
        raise RuntimeError(f"Script generation failed after {job.max_retries} attempts: {script_error}")
    initialize_plan(job)
    (store.root / job.job_id / "script.json").write_text(json.dumps(job.script, indent=2), encoding="utf-8")
    (store.root / job.job_id / "metadata.json").write_text(json.dumps({
        "title": job.script.get("title", job.topic), "language": "uk",
        "source": job.source, "character_id": job.character_id
    }, indent=2), encoding="utf-8")
    voice_path = character_root / "voice.json"
    try:
        voice_config = json.loads(voice_path.read_text(encoding="utf-8")) if voice_path.exists() else {}
    except ValueError:
        voice_config = {}
    tts = TTSAdapter(voice_config.get("provider"))
    scene_audio = []
    for tts_attempt in range(1, max(1, job.max_retries) + 1):
        transition_stage(job, StageName.TTS, StageStatus.RUNNING)
        scene_audio = []
        tts_results = []
        for scene in job.scenes:
            if not scene.voiceover or tts.provider == "none":
                continue
            audio_path = store.root / job.job_id / "audio" / f"{scene.scene_id}.wav"
            result = tts.synthesize(scene.voiceover, audio_path, scene.duration, voice_config)
            tts_results.append((scene, audio_path, result))
            if audio_path.exists():
                scene_audio.append(audio_path)
        failed_tts = next((item for _, _, item in tts_results
                           if item.get("status") in {"FAILED", "NOT_CONFIGURED"}), None)
        if not failed_tts:
            transition_stage(job, StageName.TTS, StageStatus.READY)
            break
        transition_stage(job, StageName.TTS, StageStatus.FAILED, failed_tts.get("error"))
        store.event(job, f"TTS FAILED {tts_attempt}/{job.max_retries}: {failed_tts.get('error', 'unknown error')}")
    else:
        raise RuntimeError(f"TTS failed after {job.max_retries} attempts: {failed_tts.get('error', 'unknown error')}")
    for scene, audio_path, _ in tts_results:
        if audio_path.exists():
            artifact = register_artifact(job, store.root, audio_path, "audio", scene_id=scene.scene_id,
                                         workflow=f"tts:{tts.provider}")
            scene.artifact_ids.append(artifact.artifact_id)
    if scene_audio:
        combined_audio = store.root / job.job_id / "audio" / "voice.wav"
        FFmpegAdapter().concat_audio(scene_audio, combined_audio)
        register_artifact(job, store.root, combined_audio, "audio", workflow="ffmpeg:concat")
    store.event(job, "TTS READY" if scene_audio else "TTS SKIPPED")
    store.update(job, JobStatus.SCRIPT_READY, "SCRIPT READY")
    initialize_plan(job)
    transition_stage(job, StageName.SCRIPT, StageStatus.READY)
    transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    _progress(job, "SCRIPT_READY")
    store.update(job, JobStatus.ASSET_GENERATION, "IMAGE TASK DISPATCHED")
    _progress(job, "ASSET_GENERATION")
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

def finalize_job(store: JobStore, job: Job, images: Path | list[Path]) -> Job:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return job
    image_list = [images] if isinstance(images, Path) else images
    store.update(job, JobStatus.ASSETS_READY, f"{len(image_list)} IMAGE(S) READY")
    transition_stage(job, StageName.ASSEMBLY, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSEMBLY, "ASSEMBLY STARTED")
    _progress(job, "ASSEMBLY")
    output = store.root / job.job_id / "final" / "video.mp4"
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
    if job.task_type == "video":
        FFmpegAdapter().assemble_clips(output, clips=image_list, audio=voice, subtitles=subtitles,
                                       aspect_ratio=job.aspect_ratio, preset=job.output_preset)
    else:
        FFmpegAdapter().assemble(output, images=image_list, durations=durations,
                                 audio=voice, music=music, subtitles=subtitles, aspect_ratio=job.aspect_ratio,
                                 preset=job.output_preset, watermark=watermark)
    if subtitles:
        register_artifact(job, store.root, subtitles, "subtitles", workflow="srt")
    register_artifact(job, store.root, output, "video", workflow="ffmpeg")
    job.output_path = str(output)
    store.update(job, JobStatus.VIDEO_READY, "VIDEO READY")
    transition_stage(job, StageName.ASSEMBLY, StageStatus.READY)
    store.update(job, JobStatus.READY, "JOB READY")
    _progress(job, "READY")
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
