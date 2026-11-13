"""Scene task construction, Worker selection and queueing."""

import os
import time
from pathlib import Path

import httpx

from ...dispatcher import available_worker
from ...state import (
    store,
    task_queue,
    workflow_registry,
)
from .._internal import _internal_auth_headers


def _select_worker(
        workers: list[dict],
        job,
        task_type: str | None = None,
        min_vram_mb: int | None = None,
        voice_requirements: dict | None = None):
    dispatcher_url = os.getenv("DISPATCHER_URL", "").rstrip("/")
    if not dispatcher_url:
        return available_worker(
            workers,
            job,
            task_type=task_type,
            min_vram_mb=min_vram_mb,
            voice_requirements=voice_requirements)
    try:
        payload = {"workers": workers, "job": job.model_dump(mode="json")}
        if task_type:
            payload["task_type"] = task_type
        if min_vram_mb is not None:
            payload["min_vram_mb"] = min_vram_mb
        if voice_requirements:
            payload["voice_requirements"] = voice_requirements
        response = httpx.post(f"{dispatcher_url}/select",
                              json=payload, headers=_internal_auth_headers(), timeout=3)
        response.raise_for_status()
        return response.json().get("worker")
    except (httpx.HTTPError, ValueError):
        return None


def _task_for(job, scene=None) -> dict:
    workflow = job.workflow or "workflows/image/demo.json"
    script = job.script
    task_id = job.active_task_id
    scene_id = None
    if scene is not None:
        scene_id = scene.scene_id
        task_id = scene.task_id
        script = {"scenes": [{"prompt": scene.prompt,
                              "video_prompt": scene.video_prompt,
                              "voiceover": scene.voiceover,
                              "duration": scene.duration}]}
    return {
        "job_id": job.job_id,
        "task": job.task_type,
        "priority": job.priority,
        "min_vram_mb": job.min_vram_mb,
        "workflow": workflow,
        "topic": job.topic,
        "script": script,
        "task_id": task_id,
        "scene_id": scene_id,
        **reference_task_fields(job.character_id, workflow, job.task_type)}


def reference_task_fields(character_id: str, workflow: str, task_type: str) -> dict:
    """Attach the character's references to its image tasks by default.

    A character with references must not be rendered by the plain default workflow, which
    has no image input; a workflow chosen explicitly is kept and checked on the worker.
    """
    if task_type != "image":
        return {}
    from ...character_references import list_references
    references = list_references(character_id)
    if not references:
        return {}
    if workflow == "workflows/image/demo.json":
        workflow = os.getenv("COMFYUI_REFERENCE_WORKFLOW", "workflows/image/ip-adapter.json")
    return {"workflow": workflow, "character_references": [
        {"filename": item["filename"], "sha256": item["sha256"]} for item in references]}


def _validate_workflow_reference(reference: str | None, task_type: str) -> str:
    value = reference or f"workflows/{task_type}/demo.json"
    parts = Path(value).as_posix().split("/")
    if len(parts) != 3 or parts[0] != "workflows" or parts[1] != task_type:
        raise ValueError("Workflow must use workflows/<task_type>/<name>.json")
    workflow_registry.load(parts[1], parts[2])
    return value


def _enqueue_job_task(
        job,
        scene=None,
        *,
        new_attempt: bool = False,
        delay: float = 0) -> dict:
    task = _task_for(job, scene)
    if delay:
        task["not_before"] = time.time() + delay
    queued = task_queue.enqueue(task, new_attempt=new_attempt)
    job.active_task_id = queued["task_id"]
    if scene is not None:
        scene.task_id = queued["task_id"]
        job.active_task_ids[queued["task_id"]] = scene.scene_id
    store.repository.record_task(queued, "QUEUED")
    suffix = f" FOR {scene.scene_id}" if scene is not None else ""
    store.event(job, f"TASK {queued['task_id']} QUEUED{suffix}")
    return queued
