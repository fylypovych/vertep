from __future__ import annotations

import json
import os
from pathlib import Path

from .configuration import load_character, read_json
from .models import Job, JobStatus, StoryboardScene, StoryboardVersion, utc_now
from .script_schema import normalize_script
from .storyboard_prompt import PROMPT_VERSION, build_storyboard_prompt


class StoryboardConflict(ValueError):
    pass


class StoryboardService:
    def __init__(self, store, executor=None) -> None:
        self.store = store
        self.executor = executor

    def queue(self, job: Job, revision: str | None = None) -> Job:
        job.storyboard_error = None
        self.store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")

        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id).model_dump()
        brand = read_json(Path(os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json")
        prompt = build_storyboard_prompt(job, character, brand, revision)

        version = (job.storyboards[-1].version + 1 if job.storyboards else 1)
        image_version = 1
        if job.storyboards:
            image_version = job.storyboards[-1].image_version

        task = {
            "job_id": job.job_id,
            "task": "storyboard",
            "priority": job.priority,
            "topic": job.topic,
            "prompt": prompt,
            "storyboard_version": version,
            "image_version": image_version,
            "revision": revision,
            "timeout": int(os.getenv("OLLAMA_STORYBOARD_TIMEOUT", "300")),
        }

        from .state import task_queue
        queued = task_queue.enqueue(task)
        job.storyboard_task_id = queued["task_id"]
        job.active_task_id = queued["task_id"]
        self.store.repository.record_task(queued, "QUEUED")
        self.store.event(job, f"STORYBOARD TASK {queued['task_id']} QUEUED for version {version}")

        if self.executor is not None:
            self.executor.submit(self._process_queue, job.job_id)
        else:
            self._process_queue(job.job_id)

        return job

    def _process_queue(self, job_id: str) -> None:
        from .state import task_queue
        from .queue import TaskQueue
        job = self._job(job_id)
        task_id = job.storyboard_task_id
        while True:
            task = task_queue.claim()
            if not task or task.get("task_id") != task_id:
                break
            if task.get("task") != "storyboard":
                task_queue.release(task["task_id"])
                break
            break

    def handle_result(self, job_id: str, task_id: str, success: bool, artifacts: list[dict] | None, error: str | None) -> Job:
        import base64
        job = self._job(job_id)
        if job.storyboard_task_id != task_id:
            return job

        if not success:
            attempts = max(1, int(os.getenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")))
            current_attempt = getattr(job, "storyboard_attempt", 0) + 1
            job.storyboard_attempt = current_attempt
            job.storyboard_error = error
            self.store.event(job, f"STORYBOARD FAILED {current_attempt}/{attempts}: {error}")
            if current_attempt < attempts:
                self.queue(job, job.storyboards[-1].revision_request if job.storyboards else None)
                return job
            self.store.update(job, JobStatus.STORYBOARD_FAILED,
                              f"STORYBOARD FAILED after {attempts} attempts: {error}")
            raise RuntimeError(job.storyboard_error or "Storyboard generation failed")

        artifact = next((a for a in artifacts if a["kind"] == "storyboard"), None)
        if not artifact:
            raise RuntimeError("No storyboard artifact in result")

        data = json.loads(base64.b64decode(artifact["data_base64"]).decode("utf-8"))
        scenes = [StoryboardScene(**scene) for scene in data["scenes"]]
        for scene in scenes:
            scene.scene_id = f"sb-{data['version']}-{scene.index}"
            scene.image_prompt = scene.prompt
            scene.image_version = data.get("image_version", 1)

        for previous in job.storyboards:
            if previous.status == "pending_approval":
                previous.status = "superseded"

        storyboard = StoryboardVersion(
            version=data["version"],
            title=data["title"],
            description=data["description"],
            hashtags=data["hashtags"],
            scenes=scenes,
            prompt_version=data["prompt_version"],
            model=data["model"],
            status="pending_approval",
            revision_request=data.get("revision_request"),
            image_version=data.get("image_version", 1),
            image_status=data.get("image_status", "pending"),
        )

        job.storyboards.append(storyboard)
        job.active_storyboard_version = storyboard.version
        job.active_image_version = storyboard.image_version
        job.storyboard_error = None
        job.storyboard_task_id = None
        job.active_task_id = None
        self.store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL,
                          f"STORYBOARD {storyboard.version} PENDING APPROVAL")

        try:
            from .image_storyboard import queue_image_storyboard
            queue_image_storyboard(self.store, job, storyboard.version)
            if os.getenv("LOCAL_WORKER_FALLBACK", "true").lower() == "true":
                from .image_storyboard import handle_image_result as _handle
                import base64
                demo_ppm = b"P6\n2 2\n255\n" + bytes((80, 120, 90)) * 4
                b64 = base64.b64encode(demo_ppm).decode()
                for tid in list(job.image_storyboard_task_ids.keys()):
                    _handle(self.store, job, tid, True, image_base64=b64)
                    from .state import task_queue as _tq
                    _tq.ack(tid)
                self.store.event(job, f"IMAGE STORYBOARD {storyboard.version}:{storyboard.image_version} READY (fallback)")
        except Exception as exc:
            self.store.event(job, f"IMAGE STORYBOARD QUEUE FAILED: {exc}")

        return job

    def approve(self, job_id: str, version: int, actor: str) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        if storyboard.image_status != "approved":
            raise StoryboardConflict(f"Image storyboard {version}:{storyboard.image_version} is {storyboard.image_status}; approve previews first")
        storyboard.status = "approved"
        storyboard.decided_at = utc_now()
        storyboard.decided_by = actor
        storyboard.approved_script = job.script
        job.script = normalize_script({
            "title": storyboard.title, "description": storyboard.description,
            "hashtags": storyboard.hashtags,
            "scenes": [scene.model_dump(exclude={"index", "scene_id", "artifact_id", "image_prompt", "image_artifact_id", "image_version"}) for scene in storyboard.scenes],
        }, job.topic)
        job.approved = True
        job.approval_status = "approved"
        job.version += 1
        self.store.update(job, JobStatus.STORYBOARD_APPROVED,
                          f"STORYBOARD {version} APPROVED by {actor}")
        return job

    def approve_images(self, job_id: str, version: int, actor: str) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        if any(not scene.image_artifact_id for scene in storyboard.scenes):
            raise StoryboardConflict("Not all scene preview images are ready")
        if storyboard.image_status not in {"ready", "pending"}:
            raise StoryboardConflict(f"Image storyboard is {storyboard.image_status}")
        storyboard.image_status = "approved"
        storyboard.decided_at = utc_now()
        storyboard.decided_by = actor
        job.active_image_version = storyboard.image_version
        job.version += 1
        self.store.event(job, f"IMAGE STORYBOARD {version}:{storyboard.image_version} APPROVED by {actor}")
        return job

    def request_image_revision(self, job_id: str, version: int, actor: str,
                               scene_indexes: list[int] | None = None,
                               revision: str | None = None) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        for previous in job.storyboards:
            if previous.image_status == "approved":
                previous.image_status = "superseded"
        storyboard.image_version += 1
        storyboard.image_status = "pending"
        targets = set(scene_indexes) if scene_indexes else {s.index for s in storyboard.scenes}
        for scene in storyboard.scenes:
            if scene.index not in targets:
                continue
            if scene.image_prompt:
                scene.image_prompt_history.append({
                    "prompt": scene.image_prompt,
                    "image_version": storyboard.image_version - 1,
                })
            scene.image_prompt = revision or scene.prompt
            scene.image_version = storyboard.image_version
            scene.image_artifact_id = None
        job.version += 1
        self.store.event(job, f"IMAGE STORYBOARD {version}:{storyboard.image_version} REVISION by {actor}")
        from .image_storyboard import queue_image_storyboard
        queue_image_storyboard(self.store, job, version, scene_indexes)
        return job

    def reject(self, job_id: str, version: int, actor: str) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        storyboard.status = "rejected"
        storyboard.decided_at = utc_now()
        storyboard.decided_by = actor
        for previous in reversed(job.storyboards):
            if previous.version != version and previous.status == "approved" and previous.approved_script:
                job.script = previous.approved_script
                job.approved = True
                job.approval_status = "approved"
                break
        else:
            job.approved = False
            job.approval_status = "rejected"
        job.version += 1
        return self.store.update(job, JobStatus.STORYBOARD_REJECTED,
                                 f"STORYBOARD {version} REJECTED by {actor}")

    def regenerate(self, job_id: str, version: int, actor: str,
                   revision: str | None = None) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        storyboard.status = "superseded"
        storyboard.decided_at = utc_now()
        storyboard.decided_by = actor
        self.store.update(job, JobStatus.STORYBOARD_REVISION_REQUESTED,
                          f"STORYBOARD {version} REVISION REQUESTED by {actor}")
        return self.queue(job, revision)

    def get(self, job_id: str, version: int | None = None) -> StoryboardVersion:
        job = self._job(job_id)
        selected = version or job.active_storyboard_version
        match = next((item for item in job.storyboards if item.version == selected), None)
        if not match:
            raise KeyError("Storyboard not found")
        return match

    def _job(self, job_id: str) -> Job:
        job = self.store.jobs.get(job_id)
        if not job:
            raise KeyError("Job not found")
        return job

    def _active(self, job: Job, version: int) -> StoryboardVersion:
        if job.active_storyboard_version != version:
            raise StoryboardConflict(
                f"Storyboard version {version} is stale; active version is {job.active_storyboard_version}"
            )
        storyboard = self.get(job.job_id, version)
        if storyboard.status != "pending_approval":
            raise StoryboardConflict(f"Storyboard version {version} is {storyboard.status}")
        return storyboard