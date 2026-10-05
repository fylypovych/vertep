"""Capability-specific task executors used by the universal node agent."""

import base64
import hashlib
import json
import logging
import os
import re
import time as _time
import uuid
from pathlib import Path
import httpx
from adapters.providers import providers

logger = logging.getLogger("worker.executor")


def _artifact(filename: str, kind: str, data: bytes) -> dict:
    if not data:
        raise RuntimeError(f"{kind} runtime returned an empty artifact")
    return {"filename": filename, "kind": kind,
            "data_base64": base64.b64encode(data).decode("ascii")}


# Minimal magic-byte signatures for the media Vertep accepts.  A runtime that
# answers with an HTML error page, a truncated download or an empty body must be
# rejected on the worker instead of being shipped to CORE as an artifact.
_MEDIA_SIGNATURES: dict[str, tuple[tuple[bytes, ...], str]] = {
    ".png": ((b"\x89PNG\r\n\x1a\n",), "image/png"),
    ".jpg": ((b"\xff\xd8\xff",), "image/jpeg"),
    ".jpeg": ((b"\xff\xd8\xff",), "image/jpeg"),
    ".webp": ((b"RIFF",), "image/webp"),
    ".ppm": ((b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"), "image/x-portable-pixmap"),
    ".mp4": ((b"ftyp",), "video/mp4"),
    ".webm": ((b"\x1a\x45\xdf\xa3",), "video/webm"),
    ".mov": ((b"ftyp",), "video/quicktime"),
}


def media_signature(suffix: str) -> str:
    """Return the declared mime type for a supported media suffix."""
    signatures = _MEDIA_SIGNATURES.get(suffix.lower())
    if not signatures:
        raise RuntimeError(f"Unsupported media artifact type: {suffix}")
    return signatures[1]


def verify_media(data: bytes, suffix: str) -> str:
    """Validate that ``data`` really is the media its filename claims.

    Raises ``RuntimeError`` on an empty, truncated or mismatched payload so a
    broken runtime response can never become a stored artifact.
    """
    signatures = _MEDIA_SIGNATURES.get(suffix.lower())
    if not signatures:
        raise RuntimeError(f"Unsupported media artifact type: {suffix}")
    if not data:
        raise RuntimeError(f"Empty {suffix} payload from the compute runtime")
    matches = (len(data) >= 12 and data[4:8] == b"ftyp"
               if suffix.lower() in {".mp4", ".mov"}
               else any(data.startswith(marker) for marker in signatures[0]))
    if not matches:
        raise RuntimeError(f"{suffix} payload does not match its declared media signature")
    if suffix.lower() == ".webp" and data[8:12] != b"WEBP":
        raise RuntimeError("WebP payload does not match its declared media signature")
    return signatures[1]


def _media_artifact(filename: str, kind: str, data: bytes, *, workflow: str | None = None,
                    task_type: str | None = None) -> dict:
    """Build a media artifact with a verifiable contract (sha256 + mime + size).

    CORE re-checks the contract when the result arrives, so a truncated or
    swapped payload is detected end-to-end rather than trusted.
    """
    suffix = Path(filename).suffix or (".png" if kind == "image" else ".mp4")
    mime_type = verify_media(data, suffix)
    artifact = _artifact(filename, kind, data)
    artifact["contract"] = {
        "format": "media_contract/v1",
        "kind": kind,
        "task_type": task_type or kind,
        "mime_type": mime_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "workflow": workflow or None,
        "generated_at": _time.time(),
    }
    return artifact


def execute_text(task: dict) -> list[dict]:
    endpoint = f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/generate"
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    requested_model = task.get("model") or model
    payload = {"model": requested_model, "prompt": task["topic"], "stream": False}
    stream = task.get("stream", False)
    if stream:
        payload["stream"] = True
    timeout = task.get("timeout", 120)
    if stream:
        with httpx.stream("POST", endpoint, json=payload, timeout=timeout) as response:
            response.raise_for_status()
            text_parts = []
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if "response" in data:
                    text_parts.append(data["response"])
                if data.get("done"):
                    break
            text = "".join(text_parts)
    else:
        response = httpx.post(endpoint, json=payload, timeout=timeout)
        response.raise_for_status()
        text = response.json().get("response", "")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama returned no generated text")
    return [_artifact("response.txt", "text", text.encode("utf-8"))]


def execute_script(task: dict) -> list[dict]:
    """Generate a video script on the Text Worker via Ollama.

    Mirrors the contract of ``core.script_agent.ScriptAgent.generate_script``
    but runs on the worker so CORE never performs LLM inference.  In
    ``DEMO_MODE`` a deterministic demo script is returned without any network
    call (used by local fallback / tests).
    """
    from core.script_prompt import build_script_prompt
    from core.script_schema import normalize_script

    if os.getenv("DEMO_MODE", "true").lower() == "true":
        from core.script_agent import ScriptAgent
        script = ScriptAgent().generate_script(task["topic"], task.get("system_prompt", ""), task.get("character"))
        return [_artifact("script.json", "script", json.dumps(script, ensure_ascii=False).encode("utf-8"))]

    endpoint = f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/generate"
    model = os.getenv("OLLAMA_SCRIPT_MODEL") or os.getenv("OLLAMA_MODEL", "llama3.2")

    # Build prompt with revision if provided
    base_prompt = task.get("prompt") or build_script_prompt(task["topic"], task.get("system_prompt", ""), task.get("character"))
    revision = task.get("revision")
    if revision:
        base_prompt = f"{base_prompt}\n\nREVISION INSTRUCTION: {revision}\n\nApply the above revision instruction to the script. Return the complete revised script as JSON."

    payload = {"model": model, "prompt": base_prompt, "stream": False, "format": "json"}
    timeout = int(task.get("timeout", os.getenv("OLLAMA_SCRIPT_TIMEOUT", "300")))
    response = httpx.post(endpoint, json=payload, timeout=timeout)
    response.raise_for_status()
    raw = response.json().get("response", "")
    if not raw:
        raise RuntimeError("Ollama returned empty response for script generation")
    data = json.loads(raw)
    script = normalize_script(data, task["topic"])
    return [_artifact("script.json", "script", json.dumps(script, ensure_ascii=False).encode("utf-8"))]


def execute_storyboard(task: dict) -> list[dict]:
    from core.storyboard_prompt import PROMPT_VERSION, build_storyboard_prompt
    from core.script_schema import normalize_script
    from core.configuration import load_character, read_json
    from core.models import StoryboardScene
    from pathlib import Path
    import json as _json

    endpoint = f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/generate"
    model = os.getenv("OLLAMA_STORYBOARD_MODEL") or os.getenv("OLLAMA_MODEL", "llama3.2")
    payload = {"model": model, "prompt": task["prompt"], "stream": False, "format": "json"}
    timeout = task.get("timeout", 300)
    response = httpx.post(endpoint, json=payload, timeout=timeout)
    response.raise_for_status()
    raw = response.json().get("response", "")
    if not raw:
        raise RuntimeError("Ollama returned empty response for storyboard")
    data = _json.loads(raw)
    script = normalize_script(data, task["topic"])
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
    for scene in scenes:
        scene.scene_id = f"sb-{task['storyboard_version']}-{scene.index}"
        scene.image_prompt = scene.prompt
        scene.image_version = task.get("image_version", 1)
    artifact = {
        "version": task["storyboard_version"],
        "title": script["title"],
        "description": script.get("description", ""),
        "hashtags": script.get("hashtags", []),
        "scenes": [scene.model_dump() for scene in scenes],
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "status": "pending_approval",
        "revision_request": task.get("revision"),
        "image_version": task.get("image_version", 1),
        "image_status": "pending",
    }
    return [_artifact("storyboard.json", "storyboard", _json.dumps(artifact, ensure_ascii=False).encode("utf-8"))]


def list_text_models() -> list[dict]:
    endpoint = f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/tags"
    response = httpx.get(endpoint, timeout=30)
    response.raise_for_status()
    return response.json().get("models", [])


def pull_text_model(model: str) -> dict:
    endpoint = f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/pull"
    response = httpx.post(endpoint, json={"name": model, "stream": False}, timeout=600)
    response.raise_for_status()
    return response.json()


def delete_text_model(model: str) -> None:
    endpoint = f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/delete"
    response = httpx.delete(endpoint, json={"name": model}, timeout=30)
    response.raise_for_status()


def list_voices() -> list[dict]:
    endpoint = os.getenv("TTS_URL", "http://tts:8090").rstrip("/") + "/voices"
    response = httpx.get(endpoint, timeout=30)
    response.raise_for_status()
    return response.json().get("voices", [])


def synthesize_voice(text: str, voice: str = "default", speed: int = 150) -> bytes:
    endpoint = os.getenv("TTS_URL", "http://tts:8090").rstrip("/") + "/synthesize"
    response = httpx.post(endpoint, json={"text": text, "voice": voice, "speed": speed}, timeout=180)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        encoded = response.json().get("audio_base64")
        if not encoded:
            raise RuntimeError("TTS runtime returned no audio")
        return base64.b64decode(encoded, validate=True)
    return response.content


def _resolve_voice_config(task: dict) -> dict:
    """Resolve the effective character voice configuration for a voice task.

    The character voice config (``provider``/``voice``/``language``/``model``/
    ``speed``) is what actually drives the synthesis request.  We never fall
    back to a silent placeholder: an explicitly disabled provider or an empty
    voice id is a hard error so the job can retry/fail loudly instead of
    producing a bogus audio artifact.
    """
    provider = (
        str(task.get("provider") or os.getenv("TTS_PROVIDER", "none"))
    ).strip().lower()
    if provider in {"disabled", "off", "false"}:
        raise RuntimeError(
            f"TTS provider is disabled for character "
            f"'{task.get('character_id') or 'unknown'}'; no synthesis requested"
        )
    voice = str(
        task.get("voice") or os.getenv("TTS_VOICE", "default")
    ).strip()
    if not voice:
        raise RuntimeError("Character voice has no voice identifier; cannot synthesize")
    return {
        "provider": provider or "none",
        "voice": voice,
        "language": str(task.get("language") or os.getenv("TTS_LANGUAGE", "uk")),
        "model": str(
            task.get("model") or task.get("engine") or os.getenv("TTS_MODEL") or ""
        ).strip(),
        "speed": int(task.get("speed") or os.getenv("TTS_SPEED", "150")),
        "character_id": task.get("character_id"),
        "scene_id": task.get("scene_id"),
    }


def execute_voice(task: dict) -> list[dict]:
    """Synthesize speech on the Voice Worker.

    Produces a real audio artifact carrying a verifiable ``contract`` that
    records exactly which character voice config (provider / voice / language /
    model / speed) was used and a sha256 of the resulting bytes so CORE can
    validate the artifact end-to-end.
    """
    config = _resolve_voice_config(task)
    endpoint = os.getenv("TTS_URL", "http://tts:8090").rstrip("/") + "/synthesize"
    payload = {
        "text": task["topic"],
        "voice": config["voice"],
        "speed": config["speed"],
    }
    # The character voice config parameterizes the synthesis request; the
    # runtime may ignore fields it does not support (e.g. model/language).
    if config["language"]:
        payload["language"] = config["language"]
    if config["model"]:
        payload["model"] = config["model"]
    payload["provider"] = config["provider"]
    response = httpx.post(endpoint, json=payload, timeout=180)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        body = response.json()
        encoded = body.get("audio_base64")
        if not encoded:
            raise RuntimeError("TTS runtime returned no audio")
        data = base64.b64decode(encoded, validate=True)
        mime_type = body.get("mime_type", "audio/wav")
        engine = body.get("engine", "")
        duration = body.get("duration")
    else:
        data = response.content
        mime_type = content_type or "audio/wav"
        engine = ""
        duration = None
    if not data:
        raise RuntimeError("TTS runtime returned empty audio")
    name = f"speech-{config['scene_id'] or 'voice'}.wav"
    contract = {
        "format": "audio_contract/v1",
        "provider": config["provider"],
        "voice": config["voice"],
        "language": config["language"],
        "model": config["model"] or None,
        "engine": engine or None,
        "mime_type": mime_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "text_sha256": hashlib.sha256(task["topic"].encode("utf-8")).hexdigest(),
        "duration": duration,
        "speed": config["speed"],
        "scene_id": config["scene_id"],
        "character_id": config["character_id"],
    }
    artifact = _artifact(name, "audio", data)
    artifact["contract"] = contract
    return [artifact]


def execute_publisher(task: dict) -> list[dict]:
    """Publish a video to a platform via the Publisher Worker.

    Runs the platform adapter directly (YoutubePublisher, TikTokPublisher, etc.)
    using ``PUBLISHER_MOCK`` for sandbox mode.  Returns a ``publication_receipt``
    artifact carrying platform, remote_id, url, timestamp, status/error.

    Transient failures (network errors, rate limits) raise exceptions so the
    worker marks the task as failed and CORE retries with backoff.
    Permanent failures (NOT_CONFIGURED) return a receipt with status=NOT_CONFIGURED
    and the worker marks success=True; CORE will not retry these.
    """
    import time as _time

    channel = task["channel"]
    video_path = task.get("video_path", "")
    metadata = task.get("metadata", {})
    delivery_contract = task.get("delivery_contract") or {}
    publish_intent = task.get("publish_intent") or {}
    if os.getenv("PUBLISHER_MOCK", "false").lower() == "true":
        receipt = {
            "channel": channel, "status": "PUBLISHED",
            "id": f"mock-{uuid.uuid4().hex[:12]}",
            "remote_id": f"mock-{uuid.uuid4().hex[:12]}",
            "url": f"https://example.invalid/{channel}/mock-{uuid.uuid4().hex[:8]}",
            "timestamp": _time.time(),
            "video_version": publish_intent.get("video_version"),
            "video_sha256": publish_intent.get("video_sha256"),
            "upload": {"mode": "mock", "bytes": os.path.getsize(video_path) if video_path and os.path.isfile(video_path) else 0},
        }
        return [_artifact("publication.json", "publication_receipt",
                          json.dumps(receipt, sort_keys=True).encode("utf-8"))]
    from publishers import LIVE_PUBLISHERS
    publisher = LIVE_PUBLISHERS.get(channel)
    if publisher is None or not publisher.configured():
        receipt = {"channel": channel, "status": "NOT_CONFIGURED",
                   "error": f"{publisher.credential_env if publisher else 'unknown'} is missing",
                   "timestamp": _time.time()}
        return [_artifact("publication.json", "publication_receipt",
                          json.dumps(receipt, sort_keys=True).encode("utf-8"))]
    try:
        result = publisher.publish(video_path, metadata)
    except Exception as error:
        # Transient failures (network, rate limit) should raise so worker retries
        # Permanent failures should be caught by publisher and returned in result
        error_str = str(error).lower()
        if any(keyword in error_str for keyword in ("timeout", "network", "connection", "rate limit", "429", "503", "504")):
            raise  # Transient - let worker retry
        # Other exceptions treated as permanent failure
        from core.logging_config import secret_redact
        result = {"channel": channel, "status": "FAILED", "error": secret_redact(str(error))}
    if isinstance(result, dict) and "error" in result:
        from core.logging_config import secret_redact
        result["error"] = secret_redact(result["error"])
    result.setdefault("channel", channel)
    result.setdefault("timestamp", _time.time())
    # Correlate the receipt back to the durable delivery contract so CORE can
    # match it to the publish intent (owner/lease/version) on the next attempt.
    if publish_intent.get("video_version") is not None:
        result.setdefault("video_version", publish_intent["video_version"])
    if publish_intent.get("video_sha256"):
        result.setdefault("video_sha256", publish_intent["video_sha256"])
    return [_artifact("publication.json", "publication_receipt",
                      json.dumps(result, sort_keys=True).encode("utf-8"))]


def execute_backup(task: dict) -> list[dict]:
    endpoint = os.getenv("BACKUP_URL", "http://backup-service:8092").rstrip("/") + "/snapshots"
    response = httpx.post(endpoint, json={"job_id": task["job_id"], "request": task}, timeout=600)
    response.raise_for_status()
    receipt = response.json()
    if not isinstance(receipt, dict) or not receipt.get("snapshot_id"):
        raise RuntimeError("Backup service returned no snapshot receipt")
    return [_artifact("snapshot.json", "backup_receipt",
                      json.dumps(receipt, sort_keys=True).encode("utf-8"))]


def execute_image(task: dict) -> list[dict]:
    adapter = providers.compute()
    workflow = task.get("workflow") or os.getenv("COMFYUI_DEFAULT_WORKFLOW", "workflows/image/demo.json")
    topic = task.get("topic") or task.get("prompt") or ""
    scenes = (task.get("script") or {}).get("scenes")
    if scenes:
        artifacts = []
        for index, scene in enumerate(scenes, 1):
            data, filename, kind = adapter.generate_output(workflow, scene.get("prompt") or topic, "image")
            if kind != "image":
                raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
            artifacts.append(_media_artifact(f"scene-{index:03d}{Path(filename).suffix or '.png'}",
                                            "image", data, workflow=workflow, task_type="image"))
        return artifacts
    data, filename, kind = adapter.generate_output(workflow, topic, "image")
    if kind != "image":
        raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
    return [_media_artifact(filename, "image", data, workflow=workflow, task_type="image")]


def execute_video(task: dict) -> list[dict]:
    adapter = providers.compute()
    workflow = task.get("workflow") or os.getenv("COMFYUI_DEFAULT_VIDEO_WORKFLOW", "")
    topic = task.get("topic") or task.get("prompt") or ""
    if workflow:
        try:
            data, filename, kind = adapter.generate_output(workflow, topic, "video")
            if kind != "video":
                raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
            return [_media_artifact(filename, "video", data, workflow=workflow, task_type="video")]
        except RuntimeError as error:
            if "no synthetic video workflow" not in str(error):
                raise
    scenes = (task.get("script") or {}).get("scenes") or [{"prompt": topic}]
    artifacts = []
    for index, scene in enumerate(scenes, 1):
        scene_workflow = (task.get("workflow")
                          or os.getenv("COMFYUI_DEFAULT_WORKFLOW", "workflows/image/demo.json"))
        scene_data, scene_filename, kind = adapter.generate_output(
            scene_workflow,
            scene.get("prompt") or topic, "image")
        if kind != "image":
            raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
        artifacts.append(_media_artifact(f"scene-{index:03d}{Path(scene_filename).suffix or '.png'}",
                                        "image", scene_data, workflow=scene_workflow,
                                        task_type="video"))
    return artifacts


def execute_assembly(task: dict) -> list[dict]:
    """Render the final video version of a Job on this node (Issue #122 P5/P6/P7).

    The engine decision is fixed by CORE before the task was dispatched and
    travels with it as an immutable snapshot: this node must run exactly that
    engine, and it refuses the task when its effective configuration drifted
    (P7). The result is returned as a verifiable media contract, so CORE can
    import only a complete, decodable render of the dispatched version.
    """
    snapshot = task.get("engine_snapshot") or {}
    if not snapshot:
        raise RuntimeError("Assembly task carries no engine snapshot")
    engine = providers.video_engine()
    from core.engine_config import EngineSnapshotMismatch, verify_engine_snapshot

    try:
        verify_engine_snapshot(engine, snapshot, extra={
            "aspect_ratio": str(task.get("aspect_ratio") or "16:9"),
            "preset": task.get("preset"),
            "task_type": str(task.get("task_type") or "image"),
        })
    except EngineSnapshotMismatch as error:
        # Issue #122 P5/P7: the attempt was decided against one exact configuration.
        # A node whose engine, endpoint, revision or pinned upstream drifted since
        # the dispatch refuses the render instead of silently producing the video
        # with something else.
        raise RuntimeError(
            f"{error.code}: {error}; dispatched {snapshot.get('engine_id')!r}"
        ) from error

    engine_id = str(snapshot.get("engine_id") or "")
    job_id = str(task["job_id"])
    root = _job_root()
    # A Worker does not have to share CORE's storage: the approved bytes are pulled into
    # this node's own Job root when they are not there already (Issue #122 P3).
    _deliver_inputs(task, snapshot, job_id, root)
    output = _job_path(root, job_id, str(task.get("output") or ""))
    materials = [_job_path(root, job_id, item) for item in (task.get("materials") or [])]
    audio = _job_path(root, job_id, task.get("audio")) if task.get("audio") else None
    music = _job_path(root, job_id, task.get("music")) if task.get("music") else None
    subtitles = _job_path(root, job_id, task.get("subtitles")) if task.get("subtitles") else None
    watermark = _job_path(root, job_id, task.get("watermark")) if task.get("watermark") else None
    if not materials or any(item is None or not item.is_file() for item in materials):
        raise RuntimeError(
            "Assembly materials are missing from the Job storage of this node and could "
            "not be delivered from CORE"
        )
    for label, optional in (("audio", audio), ("music", music), ("subtitles", subtitles),
                            ("watermark", watermark)):
        if optional is not None and not optional.is_file():
            raise RuntimeError(f"Assembly {label} is missing from the Job storage")
    _verify_delivered_inputs(snapshot, materials=materials, audio=audio, music=music,
                             subtitles=subtitles, watermark=watermark)

    version = int(task.get("version") or 1)
    # The render is written to an attempt-scoped temporary name; CORE promotes it
    # to the immutable version only after it verified the bytes (P3/P6).
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.{task['task_id'][:8]}.part"
    try:
        engine.render(
            temporary,
            images=None if task.get("task_type") == "video" else materials,
            clips=materials if task.get("task_type") == "video" else None,
            durations=[float(value) for value in (task.get("durations") or [])],
            audio=audio,
            music=music,
            subtitles=subtitles,
            aspect_ratio=str(task.get("aspect_ratio") or "16:9"),
            preset=task.get("preset"),
            watermark=watermark,
            task_type=str(task.get("task_type") or "image"),
            script=task.get("script"),
            submit_key=task.get("submit_key"),
        )
        data = temporary.read_bytes()
        verify_media(data, output.suffix)
        artifact = _media_artifact(output.name, "video", data,
                                   workflow=engine_id, task_type="video")
        artifact["contract"]["video_version"] = version
        artifact["contract"]["engine_id"] = engine_id
        artifact["contract"]["engine_snapshot"] = snapshot
        return [artifact]
    finally:
        if temporary.exists():
            temporary.unlink()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_delivered_inputs(snapshot: dict, *, materials, audio, music, subtitles,
                             watermark) -> None:
    """Prove the delivered inputs are the approved ones (Issue #122 P3/P5).

    The dispatching CORE records every staged reference with the SHA256 of the
    approved bytes (§9.15). Verifying them here is what makes delivery across a
    different storage root provable: a Worker does not trust its own Job directory,
    it renders exactly the input the approval was given for. A changed or missing
    digest is refused instead of rendered.
    """
    approved_materials = snapshot.get("materials")
    if not isinstance(approved_materials, list):
        raise RuntimeError("Assembly snapshot carries no approved scene inputs")
    if len(approved_materials) != len(materials):
        raise RuntimeError(
            f"Assembly materials do not match the approved snapshot: "
            f"{len(materials)} delivered, {len(approved_materials)} approved"
        )
    for entry, path in zip(approved_materials, materials):
        if _file_sha256(path) != str(entry.get("sha256") or ""):
            raise RuntimeError(
                f"Assembly scene {entry.get('reference')!r} does not match its approved digest"
            )
    inputs = snapshot.get("inputs")
    if not isinstance(inputs, dict):
        raise RuntimeError("Assembly snapshot carries no approved input references")
    for label, path in (("audio", audio), ("music", music), ("subtitles", subtitles),
                        ("watermark", watermark)):
        entry = inputs.get(label)
        if entry is None:
            if path is not None:
                raise RuntimeError(f"Assembly {label} is not part of the approved snapshot")
            continue
        if path is None:
            raise RuntimeError(f"Approved assembly {label} was not delivered")
        if _file_sha256(path) != str(entry.get("sha256") or ""):
            raise RuntimeError(f"Assembly {label} does not match its approved digest")


def _job_root() -> Path:
    return Path(os.getenv("JOB_ROOT", "jobs")).resolve()


def _job_path(root: Path, job_id: str, relative: str) -> Path | None:
    """Resolve a Job-scoped relative path, refusing anything outside the Job.

    CORE never sends an absolute path of its own filesystem (Issue #122 §6/P3):
    a path that escapes the Job directory, or an id that is not a plain directory
    name, is refused instead of being resolved.
    """
    if not relative:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", job_id):
        raise RuntimeError(f"Invalid job id in assembly task: {job_id!r}")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError(f"Assembly input must be a Job-scoped relative path: {relative!r}")
    resolved = (root / job_id / candidate).resolve()
    if not resolved.is_relative_to(root / job_id):
        raise RuntimeError(f"Assembly input escapes the Job directory: {relative!r}")
    return resolved


def _core_base_url() -> str:
    """The CORE this node registered at, used to pull approved inputs (Issue #122 P3).

    ``CORE_ADDRESS`` comes first because it is the variable the Worker itself registers
    with (``worker/service.py``) and the one ``docker-compose.worker.yml`` requires. A
    delivery base that ignored it would resolve to an empty string on a stock
    configuration, so approved inputs would never reach a node with a different
    ``JOB_ROOT``.
    """
    return (os.getenv("CORE_ADDRESS") or os.getenv("CORE_URL")
            or os.getenv("CORE_API_URL")
            or os.getenv("VERTEP_CORE_URL") or "").rstrip("/")


def _worker_auth_headers(node_name: str) -> dict[str, str]:
    """The same worker authentication CORE accepts for claim/renew/result."""
    token = os.getenv("WORKER_TOKEN") or os.getenv("NODE_API_TOKEN") or ""
    headers = {"x-vertep-node": node_name}
    if token:
        headers["x-vertep-token"] = token
    return headers


def _fetch_approved_input(task: dict, relative: str, destination: Path,
                          expected_sha256: str) -> Path:
    """Pull one approved input from CORE when this node cannot see the bytes locally.

    Issue #122 P3: the dispatching CORE owns the approved bytes, and this node may run
    with a different ``JOB_ROOT`` on another host. The transfer is authenticated like
    every other worker route and the digest from the approved snapshot is verified before
    the file is used, so a node renders exactly the approved input or refuses the attempt.
    """
    base = _core_base_url()
    node_name = str(os.getenv("NODE_NAME") or os.getenv("WORKER_NAME") or "")
    if not base or not node_name:
        raise RuntimeError(
            f"Assembly input {relative!r} is not available on this node and "
            f"CORE_ADDRESS/NODE_NAME are not configured for its delivery"
        )
    task_id = str(task.get("task_id") or "")
    url = f"{base}/api/tasks/{task_id}/inputs/{relative.lstrip('/')}"
    with httpx.Client(timeout=httpx.Timeout(60.0, read=600.0)) as client:
        response = client.get(url, headers=_worker_auth_headers(node_name))
    if response.status_code != 200:
        raise RuntimeError(
            f"CORE refused to deliver assembly input {relative!r}: "
            f"HTTP {response.status_code} {response.text[:200]}"
        )
    data = response.content
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(
            f"Delivered assembly input {relative!r} does not match its approved digest"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.delivering"
    try:
        temporary.write_bytes(data)
        # An interrupted transfer must never leave a partial file that the digest check
        # of a later run could mistake for a delivered input.
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _deliver_inputs(task: dict, snapshot: dict, job_id: str, root: Path) -> None:
    """Make every approved input of the attempt available under this node's ``JOB_ROOT``.

    Shared storage keeps working exactly as before: a file that is already there is left
    alone and only verified. When it is missing, the approved bytes are pulled from CORE
    into the same Job-scoped location, so a Worker on another host renders the approved
    input rather than failing or substituting something else. A file that exists but does
    not match its approved digest is never silently replaced — the attempt is refused by
    :func:`_verify_delivered_inputs`, so a tampered delivery is visible instead of healed.
    """
    approved: dict[str, str] = {}
    for entry in snapshot.get("materials") or []:
        reference = str(entry.get("reference") or "")
        if reference:
            approved[reference] = str(entry.get("sha256") or "")
    for entry in (snapshot.get("inputs") or {}).values():
        reference = str((entry or {}).get("reference") or "")
        if reference:
            approved[reference] = str((entry or {}).get("sha256") or "")
    if not approved:
        return
    delivered = False
    for reference, digest in approved.items():
        path = _job_path(root, job_id, reference)
        if path is None or not digest:
            raise RuntimeError(f"Assembly snapshot pins an undeliverable input: {reference!r}")
        if path.exists():
            continue
        _fetch_approved_input(task, reference, path, digest)
        delivered = True
    if delivered:
        logger.info("Assembly inputs delivered from CORE for %s", job_id)


EXECUTORS = {"text": execute_text, "script": execute_script, "voice": execute_voice,
             "publish": execute_publisher, "backup": execute_backup,
             "image": execute_image, "video": execute_video,
             "assembly": execute_assembly,
             "storyboard": execute_storyboard}
ROLE_TASKS = {"text": {"text", "script", "storyboard"}, "voice": {"voice"}, "publisher": {"publish"},
              "backup": {"backup"}, "gpu": {"image", "video", "assembly"}}


def execute_role_task(role: str, task: dict) -> list[dict]:
    task_type = task.get("task", role)
    allowed = set(ROLE_TASKS.get(role, set()))
    if role == "core":
        local_roles = {item.strip() for item in os.getenv("NODE_ADDITIONAL_ROLES", "").split(",")}
        allowed = {task for local_role in local_roles for task in ROLE_TASKS.get(local_role, set())}
    if task_type not in allowed:
        raise PermissionError(f"Role {role} is not authorized to execute {task_type}")
    executor = EXECUTORS.get(task_type)
    if not executor:
        raise ValueError(f"No role executor for task type {task_type}")
    return executor(task)
