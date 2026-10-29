"""#360: real espeak process and issued claim -> HTTP result mutation fences."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.state import store, task_queue
from tests.acceptance_provenance import acceptance_provenance
from tests import test_voice_acceptance as setup
from worker.role_executor import execute_role_task


@pytest.fixture
def runtime(monkeypatch, tmp_path, record_property):
    executable = os.getenv("ESPEAK_EXECUTABLE") or shutil.which("espeak-ng")
    if not executable:
        if os.getenv("VERTEP_REQUIRE_ESPEAK") == "1":
            pytest.fail("Actual espeak-ng is required")
        pytest.skip("Actual espeak-ng unavailable")
    monkeypatch.setenv("ESPEAK_EXECUTABLE", executable)
    record_property("espeak_binary_sha256", hashlib.sha256(Path(executable).read_bytes()).hexdigest())
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True)
    record_property("espeak_version", version.stdout.splitlines()[0])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("TTS_URL", url)
    with (tmp_path / "tts.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.tts_service:app",
            "--host", "127.0.0.1", "--port", str(port)], stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=url, timeout=30) as api:
                for _ in range(100):
                    assert process.poll() is None
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    pytest.fail("Actual TTS process did not start")
                yield api
        finally:
            process.terminate()
            process.wait(timeout=15)


def wav_measure(data):
    with wave.open(io.BytesIO(data)) as audio:
        frames = audio.readframes(audio.getnframes())
        duration = len(frames) / audio.getframerate() / audio.getnchannels() / audio.getsampwidth()
        return duration, audio.getframerate(), audio.getnchannels(), frames


def test_actual_espeak_catalog_parameters_and_measurements(runtime, record_property):
    catalog = runtime.get("/voices").json()
    assert "uk" in {v["id"] for v in catalog["voices"]}
    assert catalog["providers"] == catalog["models"] == ["espeak-ng"]
    def synth(**changes):
        return runtime.post("/synthesize", json=dict(text="Вітаю. Це перевірка швидкості мовлення. " * 3,
            voice="uk", language="uk", provider="espeak-ng", model="espeak-ng", speed=120, **changes))
    slow = synth()
    fast = runtime.post("/synthesize", json={**dict(text="Вітаю. Це перевірка швидкості мовлення. " * 3,
        voice="uk", language="uk", provider="espeak-ng", model="espeak-ng"), "speed": 240})
    assert slow.status_code == fast.status_code == 200
    slow_data, fast_data = [base64.b64decode(r.json()["audio_base64"]) for r in (slow, fast)]
    slow_duration, rate, channels, frames = wav_measure(slow_data)
    fast_duration, _, _, _ = wav_measure(fast_data)
    assert slow_duration > fast_duration * 1.3 > 0
    assert rate >= 16000 and channels == 1 and any(frames)
    record_property("slow_seconds", slow_duration)
    record_property("fast_seconds", fast_duration)
    record_property("audio_sha256", hashlib.sha256(slow_data).hexdigest())
    alternate = next(v["id"] for v in catalog["voices"] if v["id"].startswith("en-"))
    other = runtime.post("/synthesize", json={"text": "Вітаю. Це перевірка швидкості мовлення. " * 3,
        "voice": alternate, "language": "en", "provider": "espeak-ng", "model": "espeak-ng", "speed": 120})
    assert other.status_code == 200
    other_data = base64.b64decode(other.json()["audio_base64"])
    assert wav_measure(other_data)[0] > 0 and other_data != slow_data
    record_property("alternate_voice", alternate)
    record_property("alternate_voice_sha256", hashlib.sha256(other_data).hexdigest())
    for field, value in [("provider", "mock"), ("model", "uk_male"), ("voice", "unsupported"),
                         ("language", "unsupported"), ("speed", 79), ("speed", 451)]:
        payload = dict(text="Вітаю", voice="uk", language="uk", provider="espeak-ng", model="espeak-ng", speed=150)
        payload[field] = value
        assert runtime.post("/synthesize", json=payload).status_code == 422


@pytest.mark.parametrize("fault", ["contract", "sha256", "provider", "model", "speed", "snapshot", "foreign", "lease", "live_config"])
def test_issued_voice_result_before_mutation(runtime, monkeypatch, tmp_path, fault):
    monkeypatch.setenv("NODE_API_TOKEN", setup._WORKER_TOKEN)
    client = TestClient(app, headers=setup._worker_headers())
    job_id = setup._new_voice_job(client, monkeypatch, tmp_path / "characters")
    voice_path = tmp_path / "characters" / store.jobs[job_id].character_id / "voice.json"
    voice_path.write_text(json.dumps({"provider": "espeak-ng", "model": "espeak-ng", "voice": "uk", "language": "uk", "speed": 160}))
    setup._heartbeat(client, "voice-worker", role="voice", vram_mb=0, supported_tasks=["voice"],
                     capabilities=["speech_synthesis"], voice_catalog={"voices": ["uk"], "models": ["espeak-ng"]})
    setup._approve_script(client, job_id)
    setup._submit_image(client, job_id)
    # This boundary test uses the real espeak runtime while preserving the
    # existing controlled upstream fixture; no result validator is replaced.
    task = None
    for _ in range(100):
        task = client.post("/api/tasks/claim", json={"node_name": "voice-worker", "vram_mb": 0,
                          "supported_tasks": ["voice"]}).json().get("task")
        if task:
            break
        time.sleep(.05)
    assert task
    [artifact] = execute_role_task("voice", task)
    contract = artifact["contract"]
    job = store.jobs[job_id]
    (tmp_path / "characters" / job.character_id / "voice.json").write_text(json.dumps({"provider": "none", "voice": "en", "speed": 300}))
    node = "voice-worker"
    if fault == "contract":
        artifact.pop("contract")
    elif fault == "snapshot":
        job.voice_task_snapshot.clear()
    elif fault == "foreign":
        node = "foreign-worker"
    elif fault == "lease":
        task_queue.ack(task["task_id"])
    elif fault != "live_config":
        contract[fault] = "mismatch"
    before_worker = copy.deepcopy(store.workers["voice-worker"])
    before_artifacts = copy.deepcopy(job.artifacts)
    before_files = {str(p): p.read_bytes() for p in (store.root / job_id).rglob("*") if p.is_file()}
    before_queue = task_queue.inflight_has(task["task_id"])
    result = client.post("/api/tasks/result", json={"node_name": node, "job_id": job_id,
        "task_id": task["task_id"], "success": True, "artifacts": [artifact]})
    if fault == "live_config":
        assert result.status_code == 200, result.text
        assert task["task_id"] in job.completed_task_ids
    else:
        assert result.status_code in {400, 409}, result.text
        assert store.workers["voice-worker"] == before_worker
        assert job.artifacts == before_artifacts
        assert task_queue.inflight_has(task["task_id"]) == before_queue
        assert {str(p): p.read_bytes() for p in (store.root / job_id).rglob("*") if p.is_file()} == before_files
