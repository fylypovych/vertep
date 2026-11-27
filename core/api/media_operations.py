"""Queue and import supplemental media tasks on an existing Job."""

import base64
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from ..artifacts import register_artifact
from ..file_validation import validate_media_integrity
from ..models import JobStatus, utc_now
from ..state import store, task_queue
from ..system_state import dispatch_allowed
from .task_results import _text_task_lease

RESULT_TYPES = {
    "stt": ("transcript", ".json", "stt_contract/v1"),
    "lora_train": ("lora", ".safetensors", "lora_contract/v1"),
    "lipsync": ("video", ".mp4", "lipsync_contract/v1"),
}


def input_artifact(job, artifact_id, suffixes):
    artifact = next((item for item in job.artifacts if item.artifact_id == artifact_id), None)
    if artifact is None or Path(artifact.filename).suffix.lower() not in suffixes:
        raise HTTPException(422, "Unsupported input artifact")
    return artifact


def input_bytes(job, artifact):
    root = (store.root / job.job_id).resolve()
    path = (root / artifact.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Input artifact is unavailable")
    with path.open("rb") as handle:
        data = handle.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("Input artifact is too large or has changed")
    return data


def verified_voice_contract(job, audio):
    contract = audio.metadata
    if not contract and audio.kind == "audio":
        sidecar_path = Path(audio.path).with_suffix(".contract.json").as_posix()
        sidecar = next((item for item in job.artifacts if item.path == sidecar_path), None)
        if sidecar is not None:
            try:
                contract = json.loads(input_bytes(job, sidecar))
            except (ValueError, OSError) as error:
                raise HTTPException(422, "Voice contract is not readable") from error
    if (not isinstance(contract, dict) or contract.get("format") != "audio_contract/v1"
            or contract.get("sha256") != audio.sha256):
        raise HTTPException(422, "Lip-sync requires a verified audio_contract/v1 artifact")
    return contract


def enqueue_operation(job, kind, fields, *, audio=None):
    if not dispatch_allowed() or job.status in {JobStatus.CANCELLED, JobStatus.PAUSED}:
        raise HTTPException(409, "Job cannot dispatch an operation")
    if audio is not None:
        try:
            input_bytes(job, audio)
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error
    task_id = uuid4().hex
    task = {"task_id": task_id, "job_id": job.job_id, "task": kind,
            "character_id": job.character_id, "priority": job.priority, **fields}
    job.media_operations[task_id] = {
        "kind": kind, "status": "QUEUED",
        "audio_id": audio.artifact_id if audio else None,
        "audio_sha256": audio.sha256 if audio else None,
        "base_model": fields.get("base_model"),
        "references": fields.get("character_references", []),
    }
    store.event(job, f"{kind} {task_id} QUEUED")
    task_queue.enqueue(task)
    store.repository.record_task(task, "QUEUED")
    return {"task_id": task_id, "status": "QUEUED"}


def operation_delivery(job, task, operation):
    artifact_id = operation.get("audio_id")
    if not artifact_id:
        return task
    artifact = next(item for item in job.artifacts if item.artifact_id == artifact_id)
    encoded = base64.b64encode(input_bytes(job, artifact)).decode("ascii")
    if operation["kind"] == "stt":
        return {**task, "audio_base64": encoded, "filename": artifact.filename}
    return {**task, "audio_artifact": {"filename": artifact.filename,
                                      "data_base64": encoded, "contract": artifact.metadata}}


def operation_result(job, result):
    operation = job.media_operations[result.task_id]
    owned, _ = _text_task_lease(job, result.task_id, result.node_name)
    if not owned or operation["status"] != "CLAIMED":
        raise HTTPException(409, "Operation lease is no longer active")
    kind = operation["kind"]
    cancelled = result.cancelled or job.status == JobStatus.CANCELLED
    status = "CANCELLED" if cancelled else "FAILED"
    if result.success and not cancelled:
        artifact_kind, suffix, contract_format = RESULT_TYPES[kind]
        try:
            artifact, = result.artifacts
            contract = artifact["contract"]
            data = base64.b64decode(artifact["data_base64"], validate=True)
            limit = int(os.getenv("MAX_VIDEO_ARTIFACT_BYTES", "268435456"))
            if not data or len(data) > limit:
                raise ValueError("Invalid operation artifact size")
            if (contract.get("format") != contract_format or contract.get("size") != len(data)
                    or contract.get("sha256") != hashlib.sha256(data).hexdigest()):
                raise ValueError("Operation artifact does not match its contract")
            if operation.get("audio_sha256") and contract.get("audio_sha256") != operation["audio_sha256"]:
                raise ValueError("Operation result references different audio")
            if kind == "stt":
                transcript = json.loads(data)
                if (not isinstance(transcript, dict) or not isinstance(transcript.get("text"), str)
                        or not isinstance(transcript.get("segments"), list)
                        or transcript.get("format") != contract_format
                        or transcript.get("audio_sha256") != operation["audio_sha256"]):
                    raise ValueError("Invalid transcript")
                for segment in transcript["segments"]:
                    start, end = segment["start"], segment["end"]
                    if (not isinstance(segment["text"], str)
                            or not math.isfinite(start) or not math.isfinite(end)
                            or not 0 <= start <= end):
                        raise ValueError("Invalid transcript segment")
            else:
                validate_media_integrity(data, suffix)
                if kind == "lipsync" and (
                        contract.get("audio_contract_format") != "audio_contract/v1"
                        or contract.get("portrait_sha256") != operation["references"][0]["sha256"]):
                    raise ValueError("Lip-sync result references different inputs")
                if kind == "lora_train":
                    from adapters.lora_training import validate_base_model
                    model = validate_base_model(operation["base_model"])
                    if (contract.get("base_model") != model["model"]
                            or contract.get("base_model_license") != model["license"]):
                        raise ValueError("LoRA result references a different base model")
                    header_length = int.from_bytes(data[:8], "little")
                    if not 2 <= header_length <= min(len(data) - 8, 100_000_000):
                        raise ValueError("Invalid safetensors header")
                    header = json.loads(data[8:8 + header_length])
                    if not isinstance(header, dict) or not any(key != "__metadata__" for key in header):
                        raise ValueError("LoRA result has no tensors")
                if contract.get("character_id") != job.character_id:
                    raise ValueError("Operation result references a different character")
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
        destination = store.root / job.job_id / "operations" / f"{result.task_id}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(destination)
        record = register_artifact(job, store.root, destination, artifact_kind,
                                   task_id=result.task_id, node_name=result.node_name,
                                   metadata=contract)
        operation["artifact_id"] = record.artifact_id
        status = "COMPLETED"
    operation.update(status=status, error=result.error)
    store.event(job, f"{kind} {result.task_id} {status}")
    task_queue.ack(result.task_id)
    store.repository.record_task({"job_id": job.job_id, "task_id": result.task_id, "task": kind},
                                 status, result.node_name, result.error)
    worker = store.workers[result.node_name]
    worker.update(status=worker.get("desired_state") or "READY", current_job=None,
                  current_task=None, last_seen=utc_now())
    store.save_worker(worker)
    return {"task_id": result.task_id, **operation}
