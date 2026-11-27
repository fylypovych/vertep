"""Unit/integration/contract tests for MuseTalk lip-sync integration (#551)."""

from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from adapters.media_integrity import MediaIntegrityError
from adapters.providers import LipSyncProvider, MuseTalkProvider, provider_matrix, providers
from adapters.providers.lipsync_backends import LIPSYNC_CONTRACT_FORMAT
from core.api.nodes import CAPABILITY_BACKENDS
from core.character_references import save_reference
from core.dispatcher import Dispatcher, TASK_MIN_VRAM_MB, TaskRequest, available_worker
from core.models import Job, JobStatus
from core.outbound_only import inbound_port_violations
from services import musetalk_service
from worker.role_executor import execute_role_task


def _make_valid_png() -> bytes:
    """Build a minimal structurally valid 1x1 RGB PNG passing media_integrity."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_valid_mp4() -> bytes:
    """Build a minimal structurally valid ISO BMFF MP4 with a 'vide' handler box."""
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isommp41")
    hdlr = box(b"hdlr", b"\x00" * 8 + b"vide" + b"\x00" * 12)
    mdia = box(b"mdia", hdlr)
    trak = box(b"trak", mdia)
    moov = box(b"moov", trak)
    return ftyp + moov


def _make_valid_wav() -> bytes:
    """Build a minimal valid 16kHz mono 16-bit PCM RIFF/WAVE payload."""
    pcm = b"\x00\x00" * 160
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        16000,
        32000,
        2,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm


def test_lipsync_provider_registry_and_matrix(monkeypatch):
    monkeypatch.delenv("MUSETALK_URL", raising=False)
    monkeypatch.delenv("LIPSYNC_PROVIDER", raising=False)
    provider = providers.lipsync()
    assert isinstance(provider, LipSyncProvider)
    assert isinstance(provider, MuseTalkProvider)
    assert provider.provider == "musetalk"
    assert provider.configured() is False

    matrix = provider_matrix()
    assert matrix["lipsync"]["backend"] == "musetalk"
    assert matrix["lipsync"]["configured"] is False
    assert matrix["lipsync"]["env"] == "MUSETALK_URL"

    monkeypatch.setenv("MUSETALK_URL", "http://musetalk:8102")
    assert providers.lipsync().configured() is True
    assert provider_matrix()["lipsync"]["configured"] is True

    with pytest.raises(ValueError, match="Unsupported lip-sync provider"):
        providers.lipsync("unknown-lipsync")


def test_musetalk_provider_validates_inputs_and_output_digests(tmp_path):
    png = _make_valid_png()
    wav = _make_valid_wav()
    mp4 = _make_valid_mp4()

    unconfigured = MuseTalkProvider(url="")
    with pytest.raises(RuntimeError, match="MUSETALK_URL"):
        unconfigured.animate_bytes(png, wav)

    # Reject non-WAV audio and corrupt PNG before any HTTP call.
    called = []
    provider = MuseTalkProvider(url="http://musetalk:8102", post=lambda *a, **k: called.append(a))
    with pytest.raises(ValueError, match="WAV"):
        provider.animate_bytes(png, b"not-a-wav-file")
    with pytest.raises(MediaIntegrityError):
        provider.animate_bytes(b"not-a-png", wav, portrait_filename="avatar.png")
    assert called == []

    # Reject SHA256 tampering in response (fail-closed).
    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    tampered = MuseTalkProvider(
        url="http://musetalk:8102",
        post=lambda url, **kw: _FakeResp({
            "video_base64": base64.b64encode(mp4).decode("ascii"),
            "sha256": "0" * 64,
        }),
    )
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        tampered.animate_bytes(png, wav)

    # Reject corrupt MP4 bytes returned by upstream.
    corrupt = MuseTalkProvider(
        url="http://musetalk:8102",
        post=lambda url, **kw: _FakeResp({
            "video_base64": base64.b64encode(b"ftyp-junk-only").decode("ascii"),
            "sha256": hashlib.sha256(b"ftyp-junk-only").hexdigest(),
        }),
    )
    with pytest.raises(MediaIntegrityError):
        corrupt.animate_bytes(png, wav)

    # Valid round-trip via animate() writes verified file and returns contract.
    ok_provider = MuseTalkProvider(
        url="http://musetalk:8102",
        post=lambda url, **kw: _FakeResp({
            "format": LIPSYNC_CONTRACT_FORMAT,
            "engine": "musetalk",
            "license": "MIT",
            "filename": "kozak.mp4",
            "sha256": hashlib.sha256(mp4).hexdigest(),
            "audio_sha256": hashlib.sha256(wav).hexdigest(),
            "portrait_sha256": hashlib.sha256(png).hexdigest(),
            "video_base64": base64.b64encode(mp4).decode("ascii"),
            "fps": 25,
            "bbox_shift": 2,
        }),
    )
    p_file = tmp_path / "avatar.png"
    a_file = tmp_path / "speech.wav"
    out_file = tmp_path / "out.mp4"
    p_file.write_bytes(png)
    a_file.write_bytes(wav)
    contract = ok_provider.animate(p_file, a_file, out_file, fps=25, bbox_shift=2, character_id="kozak")
    assert out_file.read_bytes() == mp4
    assert contract["format"] == LIPSYNC_CONTRACT_FORMAT
    assert contract["sha256"] == hashlib.sha256(mp4).hexdigest()
    assert contract["audio_sha256"] == hashlib.sha256(wav).hexdigest()
    assert contract["portrait_sha256"] == hashlib.sha256(png).hexdigest()


def test_musetalk_service_health_and_lipsync(monkeypatch, tmp_path):
    client = TestClient(musetalk_service.app)
    png = _make_valid_png()
    wav = _make_valid_wav()
    mp4 = _make_valid_mp4()

    # Missing runtime entrypoint -> 503 fail-closed.
    monkeypatch.setattr(musetalk_service, "_musetalk_entry", lambda: None)
    assert client.get("/health").status_code == 503

    fake_script = tmp_path / "inference.py"
    fake_script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(musetalk_service, "_musetalk_entry", lambda: fake_script)

    assert client.get("/health").status_code == 503
    monkeypatch.setattr(musetalk_service, "_require_runtime", lambda: None)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["engine"] == "musetalk"
    assert health.json()["license"] == "MIT"
    assert health.json()["insightface_excluded"] is True

    # Invalid inputs -> 422.
    bad_wav = client.post("/lipsync", json={
        "portrait_base64": base64.b64encode(png).decode("ascii"),
        "portrait_filename": "avatar.png",
        "audio_base64": base64.b64encode(b"not-wav").decode("ascii"),
        "audio_filename": "speech.wav",
    })
    assert bad_wav.status_code == 422

    bad_ext = client.post("/lipsync", json={
        "portrait_base64": base64.b64encode(png).decode("ascii"),
        "portrait_filename": "avatar.png",
        "audio_base64": base64.b64encode(wav).decode("ascii"),
        "audio_filename": "speech.mp3",
    })
    assert bad_ext.status_code == 422

    # Valid call runs subprocess and returns verified lipsync_contract/v1.
    def fake_run(cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("--result_dir") + 1])
        cfg_file = Path(cmd[cmd.index("--inference_config") + 1])
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert Path(cfg["task_0"]["video_path"]).is_file()
        assert Path(cfg["task_0"]["audio_path"]).is_file()
        (out_dir / "result.mp4").write_bytes(mp4)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(musetalk_service.subprocess, "run", fake_run)
    resp = client.post("/lipsync", json={
        "portrait_base64": base64.b64encode(png).decode("ascii"),
        "portrait_filename": "avatar.png",
        "audio_base64": base64.b64encode(wav).decode("ascii"),
        "audio_filename": "speech.wav",
        "fps": 25,
        "bbox_shift": 0,
        "character_id": "kozak",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == LIPSYNC_CONTRACT_FORMAT
    assert body["sha256"] == hashlib.sha256(mp4).hexdigest()
    assert body["audio_sha256"] == hashlib.sha256(wav).hexdigest()
    assert body["portrait_sha256"] == hashlib.sha256(png).hexdigest()
    assert base64.b64decode(body["video_base64"]) == mp4


def test_worker_execute_lipsync_with_character_ref_and_audio_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "characters"))
    monkeypatch.setenv("MUSETALK_URL", "http://musetalk:8102")
    png = _make_valid_png()
    wav = _make_valid_wav()
    mp4 = _make_valid_mp4()

    save_reference("kozak", png)

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "format": LIPSYNC_CONTRACT_FORMAT,
                "engine": "musetalk",
                "license": "MIT",
                "filename": "kozak-lipsync.mp4",
                "sha256": hashlib.sha256(mp4).hexdigest(),
                "audio_sha256": hashlib.sha256(wav).hexdigest(),
                "portrait_sha256": hashlib.sha256(png).hexdigest(),
                "video_base64": base64.b64encode(mp4).decode("ascii"),
                "fps": 25,
                "bbox_shift": 0,
            }

    captured = {}

    def fake_post(_httpx, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResp()

    monkeypatch.setattr("worker.role_executor.migration_post", fake_post)

    audio_artifact = {
        "filename": "speech-scene1.wav",
        "kind": "audio",
        "data_base64": base64.b64encode(wav).decode("ascii"),
        "contract": {
            "format": "audio_contract/v1",
            "sha256": hashlib.sha256(wav).hexdigest(),
            "mime_type": "audio/wav",
        },
    }
    artifacts = execute_role_task(
        "gpu",
        {
            "task": "lipsync",
            "character_id": "kozak",
            "scene_id": "s1",
            "audio_artifact": audio_artifact,
        },
        _migration=True,
    )
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["kind"] == "video"
    assert base64.b64decode(art["data_base64"]) == mp4
    assert art["contract"]["format"] == LIPSYNC_CONTRACT_FORMAT
    assert art["contract"]["sha256"] == hashlib.sha256(mp4).hexdigest()
    assert art["contract"]["audio_sha256"] == hashlib.sha256(wav).hexdigest()
    assert art["contract"]["portrait_sha256"] == hashlib.sha256(png).hexdigest()
    assert art["contract"]["audio_contract_format"] == "audio_contract/v1"
    assert captured["url"] == "http://musetalk:8102/lipsync"

    # Tampered audio_contract/v1 digest must fail closed.
    bad_audio = dict(audio_artifact, contract={"format": "audio_contract/v1", "sha256": "f" * 64})
    with pytest.raises(RuntimeError, match="audio_contract/v1 sha256"):
        execute_role_task(
            "gpu",
            {"task": "lipsync", "character_id": "kozak", "audio_artifact": bad_audio},
            _migration=True,
        )

    # Non-GPU roles are not authorized to execute lipsync.
    with pytest.raises(PermissionError):
        execute_role_task(
            "voice",
            {"task": "lipsync", "character_id": "kozak", "audio_artifact": audio_artifact},
            _migration=True,
        )


def test_dispatcher_capability_and_vram_gating_for_lipsync():
    assert CAPABILITY_BACKENDS["avatar_lipsync"] == "MuseTalk Lip-Sync"
    assert TASK_MIN_VRAM_MB["lipsync"] >= 8192

    now = datetime.now(timezone.utc).isoformat()
    job = Job(
        job_id="job-lip-1",
        character_id="kozak",
        topic="Avatar speech",
        priority=1,
        status=JobStatus.NEW,
        created_at=now,
        task_type="lipsync",
        min_vram_mb=0,
    )

    low_vram_gpu = {
        "node_name": "gpu-low",
        "role": "gpu",
        "status": "ONLINE",
        "last_seen": now,
        "vram_mb": 4096,
        "free_vram_mb": 4096,
        "capabilities": ["avatar_lipsync", "image_generation"],
    }
    no_cap_gpu = {
        "node_name": "gpu-nocap",
        "role": "gpu",
        "status": "ONLINE",
        "last_seen": now,
        "vram_mb": 24576,
        "free_vram_mb": 20000,
        "capabilities": ["image_generation", "video_generation"],
    }
    ready_gpu = {
        "node_name": "gpu-lipsync",
        "role": "gpu",
        "status": "ONLINE",
        "last_seen": now,
        "vram_mb": 16384,
        "free_vram_mb": 12288,
        "capabilities": ["avatar_lipsync", "image_generation"],
    }

    assert available_worker([low_vram_gpu, no_cap_gpu], job, task_type="lipsync") is None
    chosen = available_worker([low_vram_gpu, no_cap_gpu, ready_gpu], job, task_type="lipsync")
    assert chosen is not None
    assert chosen["node_name"] == "gpu-lipsync"

    # Architecture 2.0 Dispatcher also enforces avatar_lipsync capability and VRAM floor.
    disp = Dispatcher()
    disp.register_node("gpu-low", {"avatar_lipsync"}, {"vram_mb": 4096}, "gpu")
    disp.register_node("gpu-high", {"avatar_lipsync"}, {"vram_mb": 12288}, "gpu")
    res = disp.assign_task(TaskRequest(task_id="lip-1", task_type="lipsync"))
    assert res.accepted is True
    assert res.worker_id == "gpu-high"


def test_musetalk_dockerfile_and_compose_outbound_only():
    dockerfile = Path("docker/musetalk/Dockerfile").read_text(encoding="utf-8")
    assert "TMElyralab/MuseTalk" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "EXPOSE 8102" in dockerfile
    # Explicitly verify insightface is filtered out and asserted absent, never pip-installed.
    assert "grep -viE" in dockerfile
    assert "find_spec('insightface')" in dockerfile
    assert "pip install insightface" not in dockerfile.lower()

    compose_path = Path("deploy/docker-compose.yml")
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    svc = compose["services"]["musetalk"]
    assert "musetalk" in svc.get("profiles", [])
    assert "ports" not in svc
    assert "8102" in [str(p) for p in svc.get("expose", [])]
    assert inbound_port_violations(compose_path) == []

    # Release workflow, bootstrap, and runtime-env updater must ship musetalk in the signed image lock
    release_yml = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "name: musetalk" in release_yml and "docker/musetalk/Dockerfile" in release_yml
    bootstrap_sh = Path("bootstrap.sh").read_text(encoding="utf-8")
    assert "VERTEP_MUSETALK_IMAGE=$(optional_image musetalk)" in bootstrap_sh
    updater = Path("scripts/update-runtime-env.py").read_text(encoding="utf-8")
    assert '"musetalk": "VERTEP_MUSETALK_IMAGE"' in updater


def test_core_queues_lipsync_with_verified_audio_and_imports_video(monkeypatch):
    from core.app import app
    from core.artifacts import register_artifact
    from core import character_references
    from core.state import store

    monkeypatch.setenv("NODE_API_TOKEN", "lipsync-integration-token")
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "false")
    client = TestClient(app, headers={"x-vertep-token": "lipsync-integration-token"})
    job = store.create("Animate portrait", "did_samogon", 5)
    portrait_digest = hashlib.sha256(_make_valid_png()).hexdigest()
    monkeypatch.setattr(character_references, "list_references", lambda _: [
        {"filename": "portrait.png", "sha256": portrait_digest},
    ])
    data = _make_valid_wav()
    audio_path = store.root / job.job_id / "audio" / "speech.wav"
    audio_path.write_bytes(data)
    audio_contract = {"format": "audio_contract/v1", "sha256": hashlib.sha256(data).hexdigest()}
    audio = register_artifact(job, store.root, audio_path, "audio", metadata=audio_contract)
    response = client.post(f"/api/jobs/{job.job_id}/lip-sync",
                           json={"audio_artifact_id": audio.artifact_id})
    assert response.status_code == 202, response.text
    worker = {"node_name": "test-lipsync", "role": "gpu", "vram_mb": 8192,
              "capabilities": ["avatar_lipsync"], "supported_tasks": ["lipsync"]}
    assert client.post("/api/workers/heartbeat", json=worker).status_code == 200
    task = client.post("/api/tasks/claim", json=worker).json()["task"]
    assert task["task_id"] == response.json()["task_id"]
    assert task["audio_contract"] == audio_contract
    assert base64.b64decode(task["audio_artifact"]["data_base64"]) == data
    video = _make_valid_mp4()
    contract = {"format": LIPSYNC_CONTRACT_FORMAT, "character_id": job.character_id,
                "audio_sha256": audio.sha256, "audio_contract_format": "audio_contract/v1",
                "portrait_sha256": portrait_digest, "size": len(video),
                "sha256": hashlib.sha256(video).hexdigest()}
    result = client.post("/api/tasks/result", json={
        "job_id": job.job_id, "task_id": task["task_id"], "node_name": "test-lipsync", "success": True,
        "artifacts": [{"contract": contract, "data_base64": base64.b64encode(video).decode()}],
    })
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "COMPLETED"
    artifact_id = result.json()["artifact_id"]
    assert client.get(f"/api/jobs/{job.job_id}/artifacts/{artifact_id}/download").content == video
