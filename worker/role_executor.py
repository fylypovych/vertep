"""Capability-specific task executors used by the universal node agent."""

import base64
import json
import os

import httpx
from adapters.comfyui import ComfyUIAdapter


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


def execute_voice(task: dict) -> list[dict]:
    endpoint = os.getenv("TTS_URL", "http://tts:8090").rstrip("/") + "/synthesize"
    response = httpx.post(endpoint, json={"text": task["topic"],
                          "voice": task.get("voice") or os.getenv("TTS_VOICE", "default")}, timeout=180)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        encoded = response.json().get("audio_base64")
        data = base64.b64decode(encoded, validate=True)
    else:
        data = response.content
    return [_artifact("speech.wav", "audio", data)]


def execute_publisher(task: dict) -> list[dict]:
    endpoint = os.getenv("PUBLISHER_URL", "http://publisher-worker:8091").rstrip("/") + "/publish"
    response = httpx.post(endpoint, json={"job_id": task["job_id"], "payload": task}, timeout=180)
    response.raise_for_status()
    receipt = response.json()
    if not isinstance(receipt, dict) or not receipt.get("publication_id"):
        raise RuntimeError("Publisher returned no publication receipt")
    return [_artifact("publication.json", "publication_receipt",
                      json.dumps(receipt, sort_keys=True).encode("utf-8"))]


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
    adapter = ComfyUIAdapter()
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
    adapter = ComfyUIAdapter()
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


EXECUTORS = {"text": execute_text, "voice": execute_voice,
             "publish": execute_publisher, "backup": execute_backup,
             "image": execute_image, "video": execute_video}
ROLE_TASKS = {"text": {"text"}, "voice": {"voice"}, "publisher": {"publish"},
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
