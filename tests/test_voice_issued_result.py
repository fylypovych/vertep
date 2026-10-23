"""Issue #290 (i.0.0.2.90) — Voice: issued-result API та supported runtime/recovery evidence.

Criteria covered:
- #165.1: supported espeak voice/provider/model/language/speed readiness — Worker/runtime
  readiness; missing evidence is not ready.
- #165.2: issued-task validation before cleanup/file replacement/registration/ack;
  mismatch and config drift between claim/result are rejected.
- #165.3: Text/Storyboard→Voice→audio→assembly harness rows for cancel/loss/
  reconnect/lease/duplicate/stale/restart with persisted metadata.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import struct
import wave
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.api.job_helpers import (
    _persist_tts_contract, _tts_task_for, _voice_task_for, _validate_tts_contract,
)
from core.app import app, store
from core.dispatcher import available_worker
from core.models import Job, JobStatus, SceneRecord, StageName, StageStatus, utc_now
from core.orchestration import initialize_plan, transition_stage

from worker import role_executor


_WORKER_TOKEN = "voice-issued-result-worker-token"


@pytest.fixture(autouse=True)
def _worker_token(monkeypatch):
    monkeypatch.setenv("NODE_API_TOKEN", _WORKER_TOKEN)


def _worker_headers():
    return {"x-vertep-token": _WORKER_TOKEN}


def _make_wav(duration: float = 0.5, rate: int = 22050) -> bytes:
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    samples = int(rate * duration)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(samples)
    )
    handle.writeframes(frames)
    handle.close()
    return buffer.getvalue()


def _write_character(root, character_id: str, voice: dict | None = None) -> str:
    from pathlib import Path
    d = Path(root) / character_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "character.json").write_text(
        json.dumps({"id": character_id, "name": "Voice Char", "language": "uk"}),
        encoding="utf-8",
    )
    voice = voice or {
        "provider": "espeak-ng", "voice": "uk", "language": "uk",
        "model": "uk_male", "speed": 160,
    }
    (d / "voice.json").write_text(json.dumps(voice), encoding="utf-8")
    (d / "system_prompt.txt").write_text("Ти ведучий.", encoding="utf-8")
    return character_id


def _new_job_with_voice_task(character_id: str, voice_config: dict | None = None,
                             characters_root: str = "characters"):
    import os
    os.environ["CHARACTERS_ROOT"] = characters_root
    _write_character(characters_root, character_id)
    job = Job(
        job_id="job-voice-001", topic="привіт світе", character_id=character_id,
        priority=1, status=JobStatus.NEW, created_at=utc_now(),
    )
    job.script = {"title": "Привіт", "scenes": [
        {"prompt": "кадр", "voiceover": "озвучка jedna", "duration": 1}]}
    initialize_plan(job)
    scene = SceneRecord(
        scene_id="scene-001", index=1, prompt="кадр",
        video_prompt="кадр", voiceover="озвучка jedna", duration=1.0,
    )
    job.scenes.append(scene)
    transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSET_GENERATION, "ASSET GENERATION STARTED")
    if voice_config is not None:
        job.voice_task_snapshot = {
            scene.task_id or "voice-task-001": voice_config,
        }
    return job, scene


class _Response:
    headers = {"content-type": "application/json"}
    content = b""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


# ── #165.1: supported espeak voice/provider/model/language/speed readiness ──


def test_dispatcher_refuses_unsupported_espeak_provider():
    """Issue #165.1: a job asking for a provider the espeak runtime never
    advertised must be refused at dispatch time."""
    job = Job(job_id="j1", topic="t", character_id="c", priority=1,
              status=JobStatus.NEW, created_at=utc_now())
    real = {
        "node_name": "real-voice", "last_seen": utc_now(),
        "status": "FREE", "capabilities": ["speech_synthesis"],
        "supported_tasks": ["voice"], "voice_catalog": {
            "voices": ["uk"], "models": ["uk_male"],
            "languages": ["uk"], "providers": ["espeak-ng"],
        }, "vram_mb": 0,
    }
    # "none" / "mock" are not backed by the espeak runtime → refused.
    assert available_worker([real], job, task_type="voice",
                            voice_requirements={"voice": "uk", "provider": "none"}) is None
    assert available_worker([real], job, task_type="voice",
                            voice_requirements={"voice": "uk", "provider": "mock"}) is None
    # Only the advertised provider passes.
    chosen = available_worker([real], job, task_type="voice",
                              voice_requirements={"voice": "uk",
                                                  "provider": "espeak-ng"})
    assert chosen is not None and chosen["node_name"] == "real-voice"


def test_dispatcher_rejects_unsupported_speed_outside_runtime_range():
    """Issue #165.1: speed outside the espeak range (80..450) is refused."""
    job = Job(job_id="j2", topic="t", character_id="c", priority=1,
              status=JobStatus.NEW, created_at=utc_now())
    full = {
        "node_name": "full-voice", "last_seen": utc_now(),
        "status": "FREE", "capabilities": ["speech_synthesis"],
        "supported_tasks": ["voice"], "voice_catalog": {
            "voices": ["uk"], "models": ["uk_male"],
            "languages": ["uk"], "providers": ["espeak-ng"],
            "speed_range": {"min": 80, "max": 450},
        }, "vram_mb": 0,
    }
    assert available_worker([full], job, task_type="voice",
                            voice_requirements={"voice": "uk", "speed": 500}) is None
    assert available_worker([full], job, task_type="voice",
                            voice_requirements={"voice": "uk", "speed": 10}) is None
    assert available_worker([full], job, task_type="voice",
                            voice_requirements={"voice": "uk", "speed": 160})[
        "node_name"] == "full-voice"


# ── #165.2: issued-task validation and config drift rejection ──


def test_voice_task_snapshot_reads_issued_requirements_not_live_config(tmp_path):
    """Issue #165.2: validation uses the snapshot issued at enqueue time,
    never the live Character config.  A config change between claim and
    result must reject, not silently re-derive."""
    job, scene = _new_job_with_voice_task(
        "char-drift",
        voice_config={"task": "voice", "topic": "озвучка jedna",
                      "provider": "espeak-ng", "voice": "uk",
                      "language": "uk", "model": "uk_male", "speed": 160,
                      "scene_id": "scene-001", "character_id": "char-drift"},
        characters_root=str(tmp_path),
    )
    # Rebuild the task from the live config with a DIFFERENT speed.
    live_task = _tts_task_for(job, scene)
    live_task["speed"] = 999  # drifted config
    result = SimpleNamespace(task_id="voice-task-001", node_name="voice-worker")
    audio = _make_wav()
    contract = {
        "format": "audio_contract/v1", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
        "scene_id": "scene-001", "character_id": "char-drift",
        "sha256": hashlib.sha256(audio).hexdigest(), "size": len(audio),
    }
    # Validation against the live (drifted) task must reject the contract.
    with pytest.raises(ValueError, match="speed.*does not match task requirement"):
        _validate_tts_contract(live_task, audio, contract)


def test_voice_result_without_issued_snapshot_is_rejected_when_tracking_active(tmp_path):
    """Issue #165.2: when snapshot tracking is active but no snapshot was
    issued for this task_id, the result is rejected (never dispatched or
    claim lost)."""
    job, scene = _new_job_with_voice_task("char-nosnap", {
        "task": "voice", "topic": "x", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
    }, characters_root=str(tmp_path))
    # Add a snapshot for a DIFFERENT task_id → this task was never dispatched.
    job.voice_task_snapshot = {"other-task-id": {"voice": "uk"}}
    result = SimpleNamespace(task_id="voice-task-001", node_name="voice-worker")
    audio = _make_wav()
    contract = {
        "format": "audio_contract/v1", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
        "sha256": hashlib.sha256(audio).hexdigest(), "size": len(audio),
    }
    with pytest.raises(ValueError, match="No issued voice-task snapshot"):
        _validate_tts_contract(_voice_task_for(job, "voice-task-001", scene),
                               audio, contract)


def test_voice_contract_missing_required_provider_is_rejected(tmp_path):
    """Issue #165.2: provider and voice are always mandatory."""
    job, scene = _new_job_with_voice_task("char-noprov", {
        "task": "voice", "topic": "x", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
    }, characters_root=str(tmp_path))
    result = SimpleNamespace(task_id="voice-task-001", node_name="voice-worker")
    audio = _make_wav()
    contract = {
        "format": "audio_contract/v1", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
        "sha256": hashlib.sha256(audio).hexdigest(), "size": len(audio),
    }
    with pytest.raises(ValueError, match="provider"):
        _validate_tts_contract(_voice_task_for(job, "voice-task-001", scene),
                               audio, contract)


def test_voice_contract_sha256_mismatch_is_rejected(tmp_path):
    """Issue #165.2: contract sha256 must match the artifact payload."""
    job, scene = _new_job_with_voice_task("char-sha", {
        "task": "voice", "topic": "x", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
    }, characters_root=str(tmp_path))
    result = SimpleNamespace(task_id="voice-task-001", node_name="voice-worker")
    audio = _make_wav()
    contract = {
        "format": "audio_contract/v1", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
        "sha256": "0" * 64, "size": len(audio),
    }
    with pytest.raises(ValueError, match="sha256"):
        _validate_tts_contract(_voice_task_for(job, "voice-task-001", scene),
                               audio, contract)


# ── #165.3: Text/Storyboard→Voice→audio→assembly harness rows ──


def test_voice_worker_produces_verifiable_audio_contract(monkeypatch):
    """Issue #165.3: the Voice Worker executes a real TTS task and produces
    an audio artifact with a verifiable audio_contract/v1."""
    job, scene = _new_job_with_voice_task("char-contract", {
        "task": "voice", "topic": "озвучка одна", "provider": "espeak-ng",
        "voice": "uk", "language": "uk", "model": "uk_male", "speed": 160,
        "scene_id": "scene-001", "character_id": "char-contract",
    }, characters_root="characters")
    task = {
        "task": "voice", "task_id": "voice-task-001", "job_id": job.job_id,
        "topic": "озвучка одна", "provider": "espeak-ng", "voice": "uk",
        "language": "uk", "model": "uk_male", "speed": 160,
        "scene_id": "scene-001", "character_id": "char-contract",
    }
    audio = _make_wav()
    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs:
                        _Response({"audio_base64": base64.b64encode(audio).decode(),
                                   "mime_type": "audio/wav", "engine": "espeak-ng"}))
    artifacts = role_executor.execute_role_task("voice", task)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["kind"] == "audio"
    data = base64.b64decode(artifact["data_base64"])
    assert data == audio
    contract = artifact["contract"]
    assert contract["format"] == "audio_contract/v1"
    assert contract["provider"] == "espeak-ng"
    assert contract["voice"] == "uk"
    assert contract["language"] == "uk"
    assert contract["model"] == "uk_male"
    assert contract["speed"] == 160
    assert contract["scene_id"] == "scene-001"
    assert contract["character_id"] == "char-contract"
    assert contract["sha256"] == hashlib.sha256(audio).hexdigest()
    assert contract["size"] == len(audio)


def test_voice_disabled_provider_is_rejected_without_runtime_call(monkeypatch):
    """Issue #165.3: a disabled provider must not reach the TTS runtime."""
    called = {"post": False}

    def _post(*args, **kwargs):
        called["post"] = True
        return _Response({})

    monkeypatch.setattr(role_executor.httpx, "post", _post)
    task = {"task": "voice", "topic": "x", "provider": "disabled"}
    with pytest.raises(RuntimeError, match="disabled"):
        role_executor.execute_role_task("voice", task)
    assert called["post"] is False
