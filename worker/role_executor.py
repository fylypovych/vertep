"""Capability-specific task executors used by the universal node agent."""

import base64
import hashlib
import json
import os
import time as _time
import uuid

import httpx
from adapters.providers import providers


def _artifact(filename: str, kind: str, data: bytes) -> dict:
    if not data:
        raise RuntimeError(f"{kind} runtime returned an empty artifact")
    return {"filename": filename, "kind": kind,
            "data_base64": base64.b64encode(data).decode("ascii")}


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
    prompt = task.get("prompt") or build_script_prompt(task["topic"], task.get("system_prompt", ""), task.get("character"))
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json"}
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
    """
    import time as _time

    channel = task["channel"]
    video_path = task.get("video_path", "")
    metadata = task.get("metadata", {})
    if os.getenv("PUBLISHER_MOCK", "false").lower() == "true":
        receipt = {
            "channel": channel, "status": "PUBLISHED",
            "id": f"mock-{uuid.uuid4().hex[:12]}",
            "remote_id": f"mock-{uuid.uuid4().hex[:12]}",
            "url": f"https://example.invalid/{channel}/mock-{uuid.uuid4().hex[:8]}",
            "timestamp": _time.time(),
            "upload": {"mode": "mock", "bytes": os.path.getsize(video_path) if video_path and os.path.isfile(video_path) else 0},
        }
        return [_artifact("publication.json", "publication_receipt",
                          json.dumps(receipt, sort_keys=True).encode("utf-8"))]
    from publishers import LIVE_PUBLISHERS
    publisher = LIVE_PUBLISHERS.get(channel)
    if publisher is None or not publisher.configured():
        return [_artifact("publication.json", "publication_receipt",
                          json.dumps({"channel": channel, "status": "NOT_CONFIGURED",
                                        "error": f"{publisher.credential_env if publisher else 'unknown'} is missing"},
                                       sort_keys=True).encode("utf-8"))]
    try:
        result = publisher.publish(video_path, metadata)
    except Exception as error:
        result = {"channel": channel, "status": "FAILED", "error": str(error)}
    result.setdefault("channel", channel)
    result.setdefault("timestamp", _time.time())
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
            artifacts.append(_artifact(f"scene-{index:03d}{Path(filename).suffix or '.png'}", "image", data))
        return artifacts
    data, filename, kind = adapter.generate_output(workflow, topic, "image")
    if kind != "image":
        raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
    return [_artifact(filename, "image", data)]


def execute_video(task: dict) -> list[dict]:
    adapter = providers.compute()
    workflow = task.get("workflow") or os.getenv("COMFYUI_DEFAULT_VIDEO_WORKFLOW", "")
    topic = task.get("topic") or task.get("prompt") or ""
    if workflow:
        try:
            data, filename, kind = adapter.generate_output(workflow, topic, "video")
            if kind != "video":
                raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
            return [_artifact(filename, "video", data)]
        except RuntimeError as error:
            if "no synthetic video workflow" not in str(error):
                raise
    scenes = (task.get("script") or {}).get("scenes") or [{"prompt": topic}]
    artifacts = []
    for index, scene in enumerate(scenes, 1):
        scene_data, scene_filename, kind = adapter.generate_output(
            task.get("workflow") or os.getenv("COMFYUI_DEFAULT_WORKFLOW", "workflows/image/demo.json"),
            scene.get("prompt") or topic, "image")
        if kind != "image":
            raise RuntimeError(f"ComfyUI workflow returned unexpected kind: {kind}")
        artifacts.append(_artifact(f"scene-{index:03d}{Path(scene_filename).suffix or '.png'}", "image", scene_data))
    return artifacts


EXECUTORS = {"text": execute_text, "script": execute_script, "voice": execute_voice,
             "publish": execute_publisher, "backup": execute_backup,
             "image": execute_image, "video": execute_video,
             "storyboard": execute_storyboard}
ROLE_TASKS = {"text": {"text", "script", "storyboard"}, "voice": {"voice"}, "publisher": {"publish"},
              "backup": {"backup"}, "gpu": {"image", "video"}}


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
