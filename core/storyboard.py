from __future__ import annotations

import json
import os
from pathlib import Path

from adapters.llm_clients import OllamaClient

from .configuration import load_character, read_json
from .models import Job, JobStatus, StoryboardScene, StoryboardVersion, utc_now
from .script_schema import normalize_script
from .storyboard_prompt import PROMPT_VERSION, build_storyboard_prompt


class StoryboardConflict(ValueError):
    pass


class StoryboardService:
    def __init__(self, store, executor=None, client_factory=None) -> None:
        self.store = store
        self.executor = executor
        self.client_factory = client_factory or self._ollama_client

    @staticmethod
    def _ollama_client():
        return OllamaClient(
            model=os.getenv("OLLAMA_STORYBOARD_MODEL") or os.getenv("OLLAMA_MODEL", "llama3.2"),
            timeout=float(os.getenv("OLLAMA_STORYBOARD_TIMEOUT", "300")),
        )

    def queue(self, job: Job, revision: str | None = None) -> Job:
        job.storyboard_error = None
        self.store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
        if self.executor is None:
            self.generate(job.job_id, revision)
        else:
            self.executor.submit(self.generate, job.job_id, revision)
        return job

    def generate(self, job_id: str, revision: str | None = None) -> StoryboardVersion:
        job = self._job(job_id)
        attempts = max(1, int(os.getenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")))
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id).model_dump()
        brand = read_json(Path(os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json")
        prompt = build_storyboard_prompt(job, character, brand, revision)
        error = None
        for attempt in range(1, attempts + 1):
            job.storyboard_attempt = attempt
            self.store.update(job, JobStatus.STORYBOARD_GENERATING,
                              f"STORYBOARD GENERATING {attempt}/{attempts}")
            try:
                client = self.client_factory()
                raw = client.complete(prompt, format_json=True)
                payload = json.loads(raw)
                script = normalize_script(payload, job.topic)
                scenes = [StoryboardScene(index=index, **scene)
                          for index, scene in enumerate(script["scenes"], 1)]
                target = float(os.getenv("STORYBOARD_TARGET_DURATION", "60"))
                tolerance = float(os.getenv("STORYBOARD_DURATION_TOLERANCE", "0.15"))
                total_duration = sum(scene.duration for scene in scenes)
                if abs(total_duration - target) > target * tolerance:
                    raise ValueError(
                        f"Storyboard duration {total_duration:g}s is outside "
                        f"{target:g}s ± {tolerance:.0%}"
                    )
                for previous in job.storyboards:
                    if previous.status == "pending_approval":
                        previous.status = "superseded"
                storyboard = StoryboardVersion(
                    version=(job.storyboards[-1].version + 1 if job.storyboards else 1),
                    title=script["title"], description=script.get("description", ""),
                    hashtags=script.get("hashtags", []), scenes=scenes,
                    prompt_version=PROMPT_VERSION, model=getattr(client, "model", ""),
                    status="pending_approval", revision_request=revision,
                )
                job.storyboards.append(storyboard)
                job.active_storyboard_version = storyboard.version
                job.storyboard_error = None
                self.store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL,
                                  f"STORYBOARD {storyboard.version} PENDING APPROVAL")
                return storyboard
            except Exception as caught:
                error = str(caught)
                job.storyboard_error = error
                self.store.event(job, f"STORYBOARD FAILED {attempt}/{attempts}: {error}")
        self.store.update(job, JobStatus.STORYBOARD_FAILED,
                          f"STORYBOARD FAILED after {attempts} attempts: {error}")
        raise RuntimeError(job.storyboard_error or "Storyboard generation failed")

    def approve(self, job_id: str, version: int, actor: str) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        storyboard.status = "approved"
        storyboard.decided_at = utc_now()
        storyboard.decided_by = actor
        job.script = normalize_script({
            "title": storyboard.title, "description": storyboard.description,
            "hashtags": storyboard.hashtags,
            "scenes": [scene.model_dump(exclude={"index"}) for scene in storyboard.scenes],
        }, job.topic)
        job.approved = True
        job.approval_status = "approved"
        job.version += 1
        self.store.update(job, JobStatus.STORYBOARD_APPROVED,
                          f"STORYBOARD {version} APPROVED by {actor}")
        return job

    def reject(self, job_id: str, version: int, actor: str) -> Job:
        job = self._job(job_id)
        storyboard = self._active(job, version)
        storyboard.status = "rejected"
        storyboard.decided_at = utc_now()
        storyboard.decided_by = actor
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
