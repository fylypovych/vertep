"""Capability-specific task executors used by the universal node agent."""

import base64
import json
import os

import httpx


def _artifact(filename: str, kind: str, data: bytes) -> dict:
    if not data:
        raise RuntimeError(f"{kind} runtime returned an empty artifact")
    return {"filename": filename, "kind": kind,
            "data_base64": base64.b64encode(data).decode("ascii")}


def execute_text(task: dict) -> list[dict]:
    response = httpx.post(f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/generate",
                          json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"),
                                "prompt": task["topic"], "stream": False}, timeout=120)
    response.raise_for_status()
    text = response.json().get("response")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama returned no generated text")
    return [_artifact("response.txt", "text", text.encode("utf-8"))]


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


EXECUTORS = {"text": execute_text, "voice": execute_voice,
             "publish": execute_publisher, "backup": execute_backup}
ROLE_TASKS = {"text": {"text"}, "voice": {"voice"}, "publisher": {"publish"},
              "backup": {"backup"}}


def execute_role_task(role: str, task: dict) -> list[dict]:
    task_type = task.get("task", role)
    if task_type not in ROLE_TASKS.get(role, set()):
        raise PermissionError(f"Role {role} is not authorized to execute {task_type}")
    executor = EXECUTORS.get(task_type)
    if not executor:
        raise ValueError(f"No role executor for task type {task_type}")
    return executor(task)
