"""Image storyboard: preview images per scene via GPU Worker/ComfyUI."""
from __future__ import annotations
import base64, binascii, os
from pathlib import Path
from .models import Job


def _image_task_for(job: Job, storyboard_version: int, scene, image_version: int) -> dict:
    try:
        from .configuration import load_character
        ch = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id)
        workflow = getattr(ch, "workflow", None)
    except Exception:
        workflow = None
    workflow = workflow or job.workflow or "workflows/image/demo.json"
    prompt = scene.image_prompt or scene.prompt or job.topic
    return {
        "job_id": job.job_id, "task": "image", "task_type": "image",
        "kind": "image_storyboard", "storyboard_version": storyboard_version,
        "image_version": image_version, "scene_index": scene.index,
        "scene_id": getattr(scene, "scene_id", None) or f"sb-{storyboard_version}-{scene.index}",
        "prompt": prompt, "topic": prompt, "workflow": workflow,
        "priority": job.priority, "min_vram_mb": job.min_vram_mb, "task_id": None,
    }


def queue_image_storyboard(store, job: Job, storyboard_version: int, scene_indexes: list[int] | None = None) -> Job:
    from .state import task_queue
    storyboard = next((s for s in job.storyboards if s.version == storyboard_version), None)
    if not storyboard:
        raise KeyError("Storyboard not found")
    storyboard.image_status = "generating"
    job.image_storyboard_error = None
    job.active_storyboard_version = storyboard_version
    job.active_image_version = storyboard.image_version
    targets = set(scene_indexes) if scene_indexes else {s.index for s in storyboard.scenes}
    queued = 0
    for scene in storyboard.scenes:
        if scene.index not in targets:
            continue
        if not scene.scene_id:
            scene.scene_id = f"sb-{storyboard_version}-{scene.index}"
        if not scene.image_prompt:
            scene.image_prompt = scene.prompt
        scene.image_version = storyboard.image_version
        task = _image_task_for(job, storyboard_version, scene, storyboard.image_version)
        enqueued = task_queue.enqueue(task)
        job.image_storyboard_task_ids[enqueued["task_id"]] = scene.scene_id
        store.repository.record_task(enqueued, "QUEUED", None)
        queued += 1
    store.event(job, f"IMAGE STORYBOARD {storyboard_version}:{storyboard.image_version} QUEUED {queued} scene(s)")
    return job


def handle_image_result(store, job: Job, task_id: str, success: bool, image_base64: str | None = None,
                        error: str | None = None, artifacts: list[dict] | None = None) -> Job:
    from .artifacts import register_artifact
    from .file_validation import validate_signature
    scene_id = job.image_storyboard_task_ids.get(task_id)
    storyboard = next((s for s in job.storyboards if s.version == job.active_storyboard_version), None)
    scene = next((s for s in (storyboard.scenes if storyboard else []) if s.scene_id == scene_id), None) if storyboard else None
    if not success:
        if storyboard:
            storyboard.image_status = "pending"
        job.image_storyboard_error = error or "unknown error"
        job.image_storyboard_task_ids.pop(task_id, None)
        store.event(job, f"IMAGE STORYBOARD SCENE {scene.index if scene else '?'} FAILED: {error}")
        return job
    raw_images: list[tuple[str, bytes]] = []
    if artifacts:
        for item in artifacts:
            b64 = item.get("data_base64") or item.get("image_base64") or ""
            fname = item.get("filename") or f"sb-{storyboard.version}-{scene.index}.png"
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"Invalid base64: {exc}") from exc
            validate_signature(data, Path(fname).suffix.lower() or ".png")
            raw_images.append((fname, data))
    elif image_base64:
        try:
            data = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Invalid base64: {exc}") from exc
        fname = f"sb-{storyboard.version}-{scene.index}.png"
        validate_signature(data, ".png")
        raw_images.append((fname, data))
    if not raw_images:
        raise ValueError("No image data provided")
    fname, data = raw_images[0]
    folder = store.root / job.job_id / "storyboard" / f"v{storyboard.image_version}"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(fname).suffix or ".png"
    safe_name = f"scene-{scene.index:03d}-v{storyboard.image_version}{suffix}"
    path = folder / safe_name
    path.write_bytes(data)
    artifact = register_artifact(job, store.root, path, "storyboard_image",
                                 scene_id=scene.scene_id, task_id=task_id,
                                 workflow=storyboard.prompt_version if storyboard else "")
    scene.image_artifact_id = artifact.artifact_id
    scene.artifact_id = artifact.artifact_id
    job.image_storyboard_task_ids.pop(task_id, None)
    if storyboard and all(s.image_artifact_id for s in storyboard.scenes):
        storyboard.image_status = "ready"
        store.event(job, f"IMAGE STORYBOARD {storyboard.version}:{storyboard.image_version} READY")
    return job
