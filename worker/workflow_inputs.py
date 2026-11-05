"""Turn the outputs of earlier dynamic-workflow steps into the input each executor reads.

Only tasks of the dynamic workflow pass through here; the static pipeline keeps building
its tasks in CORE.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

# ponytail: artifacts travel as base64 inside the workflow state document, so long videos
# are memory-heavy; move them to shared storage references if real renders outgrow this.


def _artifacts(task: dict) -> list[dict]:
    found = []
    for output in (task.get("dependencies") or {}).values():
        if isinstance(output, list):
            found.extend(item for item in output if isinstance(item, dict))
    return found


def _bytes(artifact: dict) -> bytes:
    return base64.b64decode(artifact["data_base64"], validate=True)


def _script(artifacts: list[dict]) -> dict | None:
    for artifact in artifacts:
        if artifact.get("kind") == "script":
            return json.loads(_bytes(artifact))
    return None


def _store(destination: Path, data: bytes) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _prepare_voice(task: dict, script: dict | None) -> None:
    scenes = (script or {}).get("scenes") or []
    text = (script or {}).get("voiceover") or " ".join(str(scene.get("voiceover") or "") for scene in scenes).strip()
    if not text:
        raise RuntimeError("Workflow voice step received no script text")
    task["topic"] = text


def _prepare_assembly(task: dict, artifacts: list[dict], script: dict | None) -> None:
    from worker.role_executor import providers
    from core.engine_config import engine_snapshot_fields
    from worker.role_executor import _job_root

    root = _job_root() / str(task["job_id"])
    videos = [item for item in artifacts if item.get("kind") == "video"]
    images = [item for item in artifacts if item.get("kind") == "image"]
    sources = videos or images
    if not sources:
        raise RuntimeError("Workflow assembly step received no images or video")
    materials = []
    for item in sources:
        reference = f"workflow/{Path(item['filename']).name}"
        materials.append({"reference": reference, "sha256": _store(root / reference, _bytes(item))})
    inputs = {}
    audio = next((item for item in artifacts if item.get("kind") == "audio"), None)
    if audio:
        reference = f"workflow/{Path(audio['filename']).name}"
        inputs["audio"] = {"reference": reference, "sha256": _store(root / reference, _bytes(audio))}
        task["audio"] = reference
    scenes = (script or {}).get("scenes") or []
    task.update(materials=[entry["reference"] for entry in materials],
                durations=[float((scenes[index] if index < len(scenes) else {}).get("duration") or 5)
                           for index in range(len(materials))],
                task_type="video" if videos else "image", output="workflow/final.mp4",
                version=int(task.get("revision") or 1), script=script, submit_key=task["task_id"],
                engine_snapshot={**engine_snapshot_fields(providers.video_engine()),
                                 "materials": materials, "inputs": inputs})


def _prepare_publish(task: dict, artifacts: list[dict], script: dict | None) -> None:
    from worker.role_executor import _job_root

    video = next((item for item in artifacts if item.get("kind") == "video"), None)
    if video is None:
        raise RuntimeError("Workflow publish step received no assembled video")
    channel = task.get("channel") or os.getenv("PUBLISHER_DEFAULT_CHANNEL")
    if not channel:
        raise RuntimeError("Workflow publish step has no channel: set channel or PUBLISHER_DEFAULT_CHANNEL")
    data = _bytes(video)
    name = Path(video["filename"]).name
    # The approved bytes arrive with the task, so the Publisher never asks CORE for a file.
    digest = _store(_job_root() / str(task["job_id"]) / "publish-inputs" / name, data)
    version = int(task.get("revision") or 1)
    task.update(channel=channel, video_path=name,
                metadata={key: (script or {}).get(key) for key in ("title", "description", "hashtags")},
                publish_intent={"job_id": task["job_id"], "channel": channel,
                                "video_version": version, "video_sha256": digest},
                delivery_contract={"sha256": digest, "size_bytes": len(data), "version": version})


def prepare_workflow_task(task: dict) -> dict:
    prepared = dict(task)
    artifacts = _artifacts(task)
    script = _script(artifacts)
    if script is not None:
        prepared["script"] = script
    kind = task.get("task")
    if kind == "voice":
        _prepare_voice(prepared, script)
    elif kind == "assembly":
        _prepare_assembly(prepared, artifacts, script)
    elif kind == "publish":
        _prepare_publish(prepared, artifacts, script)
    return prepared


def require_published(task: dict, artifacts: list[dict]) -> None:
    """A receipt that is not PUBLISHED must fail the step, not let the workflow complete."""
    if task.get("task") != "publish":
        return
    receipt = next((item for item in artifacts if item.get("kind") == "publication_receipt"), None)
    body = json.loads(_bytes(receipt)) if receipt else {}
    if body.get("status") != "PUBLISHED":
        raise RuntimeError(f"Publication failed: {body.get('status')} {body.get('error') or ''}".strip())
