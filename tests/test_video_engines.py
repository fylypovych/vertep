from adapters.providers.base import (
    BridgeContractError,
    BRIDGE_SCHEMA_VERSION,
    REASON_SCHEMA_UNSUPPORTED,
    REASON_SUBMIT_UNKNOWN,
    REASON_TASK_ABSENT,
    REASON_UPSTREAM_UNREACHABLE,
    REASON_VOICE_STAGING_UNSUPPORTED,
    RELEASE_NOT_APPLIED,
    RELEASE_RELEASED,
    RELEASE_UNCONFIRMED,
    FIXED_SUBMIT_FIELDS,
)
from adapters.providers.video_engines import (
    MONEY_PRINTER_CONTRACT,
    RemoteVideoEngine,
)
from adapters.providers.runtime_manifest import load_manifest, generate_sbom
"""Phase 5 tests: VideoEngine pattern (native default + opt-in external engines).

Covers the opt-in/fallback behaviour of the video-engine factory, the native
FFmpeg render path, the remote (MoneyPrinter / ShortGPT) HTTP flow against a
``FakeTransport``, and parity between the native and external engines when
driven from the **same** Job assets.

Also covers the pinned MoneyPrinterTurbo bridge contract (Issue #122 P1):
capability declaration, pre-dispatch validation of every mandatory Job input,
and the request/status mappings of the pinned upstream API.

Issue #122 P2 tests: material upload, health check, runtime manifest.
"""

import hashlib
import json
from pathlib import Path
import gzip
import math
import struct
import wave
import zlib

import httpx
import pytest

from adapters.providers import (
    DefaultAssemblyProvider,
    get_providers,
    _make_video_engine,
    MoneyPrinterEngine,
    NativeVertepEngine,
    ShortGPTEngine,
    VideoEngine,
)
from adapters.ffmpeg import FFmpegAdapter
from publishers.transport import FakeTransport


def _image(path):
    path.write_bytes(b"P6\n4 4\n255\n" + bytes((10, 20, 30)) * 16)
    return path


def _png(path, width: int = 640, height: int = 640):
    raw = b"".join(
        b"\x00" + bytes((10, 20, 30)) * width for _ in range(height)
    )

    def chunk(tag, payload):
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _wav(path, seconds: float = 2.0, rate: int = 16000):
    tone = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * index / rate)))
        for index in range(int(rate * seconds))
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(tone)
    return path


def _mp4(path):
    path.write_bytes(
        b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
        + b"\x00" * 64
    )
    return path


def _job_assets(tmp_path):
    images = [_png(tmp_path / "scene.png")]
    return {
        "images": images,
        "clips": None,
        "durations": [2.0],
        "audio": _wav(tmp_path / "voice.wav"),
        "music": None,
        "subtitles": None,
        "aspect_ratio": "9:16",
        "preset": None,
        "watermark": None,
        "task_type": "image",
        "script": APPROVED_SCRIPT,
    }


APPROVED_SCRIPT = "Сцена: Vertep готує схвалений короткий текст для озвучення."


def _runtime_body(task_local_voice: bool = True, **overrides) -> dict:
    """Wrapper ``/runtime`` snapshot as the bridge reads it."""
    body = {
        "service": "vertep-moneyprinter",
        "upstream_commit": MONEY_PRINTER_CONTRACT.upstream_reference.split("@")[1],
        "bridge_schema_version": BRIDGE_SCHEMA_VERSION,
        "capabilities": {
            "task_local_voice": task_local_voice,
            "reason": None if task_local_voice else "upstream_voice_staging_unsupported",
        },
    }
    body.update(overrides)
    return body


def _staged_voice(audio) -> httpx.Response:
    """Wrapper answer to the approved-voice staging call (§9.3 row 5).

    The runtime stores the approved bytes itself and answers with a
    content-addressed reference, never with a path CORE could not read.
    """
    payload = Path(audio).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    return httpx.Response(200, json={
        "status": 200,
        "message": "success",
        "data": {"voice": f"vertep-voice:{digest}.wav", "sha256": digest, "bytes": len(payload)},
    })


@pytest.fixture(scope="module")
def mp4_factory(tmp_path_factory):
    """Bytes of a real, decodable MP4 of a given length (the runtime result)."""
    root = tmp_path_factory.mktemp("rendered")

    def build(seconds: float = 2.0) -> bytes:
        target = root / f"result-{seconds}.mp4"
        if not target.exists():
            NativeVertepEngine().render(
                target,
                images=[_png(root / f"result-{seconds}.png")],
                durations=[seconds],
                audio=_wav(root / f"result-{seconds}.wav", seconds=seconds),
                aspect_ratio="9:16",
            )
        return target.read_bytes()

    return build


# ---------------------------------------------------------------------------
# Factory: explicit opt-in, no substitution
# ---------------------------------------------------------------------------


def test_video_engine_default_is_native(monkeypatch):
    monkeypatch.delenv("VERTEP_VIDEO_ENGINE", raising=False)
    engine = _make_video_engine()
    assert isinstance(engine, NativeVertepEngine)


def test_video_engine_is_not_substituted_when_selected_engine_lacks_an_endpoint(monkeypatch):
    """Issue #122 §9.12: a selected engine is never silently replaced by Native.

    The engine object is still the one that was asked for, so it refuses the render with
    a readiness reason and no job is assembled by an engine the operator did not choose.
    """
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "")

    engine = _make_video_engine()

    assert isinstance(engine, MoneyPrinterEngine)
    assert engine.configured() is False


def test_video_engine_enables_money_printer_when_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://engine:8000")
    assert isinstance(_make_video_engine(), MoneyPrinterEngine)


def test_video_engine_enables_shortgpt_when_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "shortgpt")
    monkeypatch.setenv("SHORTGPT_URL", "http://engine:9000")
    assert isinstance(_make_video_engine(), ShortGPTEngine)


def test_video_engine_keeps_shortgpt_selected_without_an_endpoint(monkeypatch):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "shortgpt")
    monkeypatch.setenv("SHORTGPT_URL", "")
    assert isinstance(_make_video_engine(), ShortGPTEngine)


# ---------------------------------------------------------------------------
# Native engine
# ---------------------------------------------------------------------------


def test_native_engine_renders_real_video(tmp_path):
    assets = _job_assets(tmp_path)
    output = tmp_path / "final" / "video.mp4"
    NativeVertepEngine().render(output, **assets)
    assert output.stat().st_size > 100
    assert b"ftyp" in output.read_bytes()[:32]
# ---------------------------------------------------------------------------
# Remote (external) engine against FakeTransport
# ---------------------------------------------------------------------------


def test_remote_engine_isolation_not_configured_without_url(monkeypatch, tmp_path):
    monkeypatch.setenv("MONEY_PRINTER_URL", "")
    engine = MoneyPrinterEngine()
    assert engine.configured() is False
    with pytest.raises(RuntimeError):
        engine.render(tmp_path / "v.mp4", **(_job_assets(tmp_path)))


def test_submit_matches_the_fixed_bridge_contract(tmp_path, mp4_factory):
    """§9.4: materials carry their scene duration, clip duration is ceil(max).

    The previous implementation passed ``int(durations[0])`` and ``duration:
    None``, so a 3.4 s scene rendered as 3 s clips and the upstream received a
    receive window it could not honour.
    """
    assets = _job_assets(tmp_path)
    assets["durations"] = [3.4]
    assets["audio"] = _wav(tmp_path / "voice-3s.wav", seconds=3.4)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "a.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j1"}}),
        httpx.Response(200, json={"data": {"state": 1, "videos": ["tasks/j1/final-1.mp4"]}}),
        httpx.Response(200, content=mp4_factory(3.4)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    engine.render(tmp_path / "final" / "external.mp4", **assets)

    # GET /runtime, POST upload, POST voice, POST submit, GET status, GET download
    submit = fake.requests[3][2]["json"]
    assert submit["video_clip_duration"] == 4
    assert submit["video_materials"][0]["duration"] == 3.4


def test_clip_duration_never_exceeds_the_upstream_limit(tmp_path, mp4_factory):
    assets = _job_assets(tmp_path)
    assets["durations"] = [15.0]
    assets["audio"] = _wav(tmp_path / "voice-15s.wav", seconds=15.0)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "a.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j1"}}),
        httpx.Response(200, json={"data": {"state": 1, "videos": ["tasks/j1/final-1.mp4"]}}),
        httpx.Response(200, content=mp4_factory(15.0)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    engine.render(tmp_path / "final" / "external.mp4", **assets)

    assert fake.requests[3][2]["json"]["video_clip_duration"] == 15


def test_submit_sends_every_fixed_submission_value(tmp_path, mp4_factory):
    """§9.4 fixed set: no LLM, no TTS, no subtitles, no BGM, no cross-post."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "a.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j1"}}),
        httpx.Response(200, json={"data": {"state": 1, "videos": ["tasks/j1/final-1.mp4"]}}),
        httpx.Response(200, content=mp4_factory(2.0)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    engine.render(tmp_path / "final" / "external.mp4", **assets)

    submit = fake.requests[3][2]["json"]
    assert submit["video_source"] == "local"
    assert submit["video_terms"] is None
    assert submit["video_fit_mode"] == "contain"
    assert submit["video_concat_mode"] == "sequential"
    assert submit["video_transition_mode"] is None
    assert submit["video_clip_speed"] == 1.0
    assert submit["match_materials_to_script"] is False
    assert submit["video_count"] == 1
    assert submit["bgm_type"] == ""
    assert submit["bgm_file"] == ""
    assert submit["bgm_volume"] == 0.0
    assert submit["subtitle_enabled"] is False
    assert submit["video_language"] == ""
    assert submit["n_threads"] == 2
    assert submit["video_script"] == APPROVED_SCRIPT


def test_render_refuses_a_runtime_that_cannot_stage_the_approved_voice(tmp_path):
    """§9.6: without task-local voice staging the runtime would run its own TTS."""
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body(task_local_voice=False)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "refused.mp4", **(_job_assets(tmp_path)))
    assert exc.value.code == REASON_VOICE_STAGING_UNSUPPORTED
    # Refused pre-dispatch: nothing was uploaded, submitted or polled.
    assert [request[0] for request in fake.requests] == ["GET"]
    assert not (tmp_path / "final" / "refused.mp4").exists()


def test_render_refuses_without_the_approved_script(tmp_path):
    """§9.4: an empty script would let the runtime generate narration text."""
    fake = FakeTransport([])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assets = _job_assets(tmp_path)
    assets["script"] = "   "
    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "refused.mp4", **assets)
    assert exc.value.code == "script_unavailable"
    assert fake.requests == []


def test_render_refuses_a_mixed_media_type_job(tmp_path):
    """§9.3: mixed image and video materials are not a supported executor input."""
    assets = _job_assets(tmp_path)
    assets["images"] = []
    assets["clips"] = [_mp4(tmp_path / "scene.mp4")]
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=FakeTransport([]))
    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "refused.mp4", **assets)
    assert exc.value.code in {
        "material_channel_mismatch",
        "mixed_media_type",
        "unsupported_media_type",
    }


def test_remote_engine_http_flow(tmp_path, mp4_factory):
    """Test remote engine HTTP flow with material upload (Issue #122 P2)."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        # Runtime capability snapshot (approved voice staging)
        httpx.Response(200, json=_runtime_body()),
        # Material upload response
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        # Approved voice staging response
        _staged_voice(assets["audio"]),
        # Submit task response
        httpx.Response(200, json={"task_id": "j1"}),
        # Status check response (for MoneyPrinterEngine - map_upstream_status)
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j1/final-1.mp4"]}),
        # Download response
        httpx.Response(200, content=mp4_factory(2.0)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    output = engine.render(tmp_path / "final" / "external.mp4", **assets)

    # §9.5: the downloaded render is finished exactly like a Native render, so the
    # final artifact is the post-step output, not the raw upstream download.
    assert output.stat().st_size > 0
    assert output.read_bytes() != mp4_factory(2.0)
    methods = [r[0] for r in fake.requests]
    # GET runtime, POST upload, POST voice, POST submit, GET status, GET download
    assert methods == ["GET", "POST", "POST", "POST", "GET", "GET"]
    assert fake.requests[0][1].endswith("/runtime")
    assert fake.requests[1][1].endswith("/api/v1/video_materials")
    assert fake.requests[2][1].endswith("/api/v1/voice")
    assert fake.requests[3][1].endswith("/api/v1/videos")
    assert fake.requests[4][1].endswith("/api/v1/tasks/j1")
    # Download endpoint uses the contract template
    assert "/api/v1/download/" in fake.requests[5][1]
    # The approved voice travels as raw bytes plus its name, never as a CORE path.
    voice_call = fake.requests[2]
    assert voice_call[2]["headers"]["x-vertep-filename"] == Path(assets["audio"]).name
    assert voice_call[2]["content"] == Path(assets["audio"]).read_bytes()


class _RecordingAssembly:
    """Wraps the real assembly provider and records where staging happened."""

    def __init__(self, inner):
        self._inner = inner
        self.staging_dirs = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name != "pre_cut":
            return attribute

        def pre_cut(source, duration, output):
            self.staging_dirs.append(output.parent)
            return attribute(source, duration, output)

        return pre_cut


def _mpt_runtime_responses(result_bytes, task_id="j1", material="uploaded-scene.mp4", audio=None):
    return [
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": material}}),
        _staged_voice(audio),
        httpx.Response(200, json={"status": 200, "data": {"task_id": task_id}}),
        httpx.Response(200, json={"data": {"state": 1, "videos": [f"tasks/{task_id}/final-1.mp4"]}}),
        httpx.Response(200, content=result_bytes),
    ]


def test_each_attempt_owns_and_removes_its_staging_area(tmp_path, mp4_factory):
    """P3: staging is attempt-scoped and never outlives the attempt."""
    assets = _job_assets(tmp_path)
    assembly = _RecordingAssembly(
        DefaultAssemblyProvider(FFmpegAdapter())
    )
    engine = MoneyPrinterEngine(
        url="http://engine:8000", transport=FakeTransport([]), assembly=assembly,
    )

    for attempt, task_id in enumerate(("j1", "j2"), start=1):
        engine._transport.responses = _mpt_runtime_responses(
            mp4_factory(2.0), task_id=task_id, audio=assets["audio"],
        )
        engine.render(
            tmp_path / "final" / f"video-v{attempt}.mp4", **assets,
        )

    assert len(assembly.staging_dirs) == 2
    assert assembly.staging_dirs[0] != assembly.staging_dirs[1]
    # Both attempt directories are gone once the attempts finished.
    for directory in assembly.staging_dirs:
        assert not directory.exists()
    assert not list((tmp_path / "final").glob(".*-bridge"))


def test_a_failed_attempt_also_removes_its_staging_area(tmp_path):
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(500, json={"error": {"message": "storage offline"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    with pytest.raises(BridgeContractError):
        engine.render(tmp_path / "final" / "video-v1.mp4", **(_job_assets(tmp_path)))

    assert not list((tmp_path / "final").glob(".*-bridge"))


def test_submission_never_leaks_a_core_filesystem_path(tmp_path, mp4_factory):
    """P3: the runtime is addressed by storage keys, not by CORE paths."""
    assets_root = tmp_path / "core-storage"
    assets_root.mkdir()
    images = [_png(assets_root / "scene.png")]
    assets = _job_assets(tmp_path)
    assets["images"] = images
    assets["audio"] = _wav(assets_root / "voice.wav")
    fake = FakeTransport(_mpt_runtime_responses(mp4_factory(2.0), audio=assets["audio"]))
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    # The staging root of the Job is a different tree than the CORE storage root.
    engine.render(tmp_path / "jobs" / "2026-000001" / "final" / "video-v1.mp4", **assets)

    submit = fake.requests[3][2]["json"]
    assert str(assets_root) not in json.dumps(submit)
    assert submit["video_materials"][0]["url"] == "uploaded-scene.mp4"
    # The approved voice is submitted as a staged content address, not as a path.
    assert submit["custom_audio_file"] == (
        "vertep-voice:"
        f"{hashlib.sha256(Path(assets['audio']).read_bytes()).hexdigest()}.wav"
    )
    assert submit["video_materials"][0]["original_name"].startswith("scene-")


def test_verified_import_is_readable_and_checksummed(tmp_path, mp4_factory):
    """P3: checksum, size and access are all verified on the imported artifact."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport(_mpt_runtime_responses(mp4_factory(2.0), audio=assets["audio"]))
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    output = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert output.is_file() and output.stat().st_size > 0
    assert engine._verify_readable(output) == output.stat().st_size
    sidecar = output.with_suffix(output.suffix + ".sha256")
    assert sidecar.read_text(encoding="utf-8") == hashlib.sha256(
        output.read_bytes()
    ).hexdigest()
    assert not output.with_suffix(output.suffix + ".tmp").exists()


def test_an_unreadable_import_is_never_registered(tmp_path, mp4_factory, monkeypatch):
    assets = _job_assets(tmp_path)
    fake = FakeTransport(_mpt_runtime_responses(mp4_factory(2.0), audio=assets["audio"]))
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    real_open = Path.open

    def guarded(self, mode="r", *args, **kwargs):
        if "r" in mode and self.name == "video-v1.mp4":
            raise OSError("permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)

    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert exc.value.code == "upstream_result_corrupted"
    assert "not readable" in str(exc.value)


def test_remote_engine_sends_bearer_token(tmp_path):
    """ShortGPTEngine uses legacy flow without material upload (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"job_id": "j1"}),
        httpx.Response(200, json={"status": "FAILED", "error": "oom"}),
    ])
    engine = ShortGPTEngine(
        url="http://engine:9000", token="s3cret", transport=fake
    )
    with pytest.raises(RuntimeError) as exc:
        engine.render(tmp_path / "v.mp4", **(_job_assets(tmp_path)))
    assert "oom" in str(exc.value)
    assert fake.requests[0][2]["headers"]["Authorization"] == "Bearer s3cret"
    # ShortGPTEngine uses legacy /render endpoint
    assert fake.requests[0][1].endswith("/render")


def test_remote_engine_api_error_surfaces_http_code(tmp_path):
    """ShortGPTEngine (legacy flow) surfaces HTTP errors (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(400, json={"error": {"message": "engine down"}}),
    ])
    engine = ShortGPTEngine(url="http://engine:9000", transport=fake)
    with pytest.raises(RuntimeError) as exc:
        engine.render(tmp_path / "v.mp4", **(_job_assets(tmp_path)))
    assert "400" in str(exc.value)


def test_moneyprinter_upload_error_surfaces(tmp_path):
    """MoneyPrinterEngine material upload error surfaces before submit (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(400, json={"error": {"message": "storage full"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "v.mp4", **(_job_assets(tmp_path)))
    assert exc.value.code == "upstream_upload_failed"
    assert "storage full" in str(exc.value)


def test_remote_engine_contract_negative_cases(tmp_path):
    engine = MoneyPrinterEngine(url="http://engine:8000")
    assets = _job_assets(tmp_path)

    # Invalid aspect ratio validation
    assets_invalid_ar = dict(assets)
    assets_invalid_ar["aspect_ratio"] = "21:9"
    with pytest.raises(ValueError) as exc:
        engine.render(tmp_path / "v.mp4", **assets_invalid_ar)
    assert "aspect ratio" in str(exc.value)

    # Capabilities structure check
    caps = engine.capabilities()
    assert caps["schema_version"] == "v1"
    assert "16:9" in caps["supported_aspect_ratios"]


def test_remote_engine_artifact_delivery_empty_download(tmp_path):
    """ShortGPTEngine (legacy flow) rejects empty download (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"job_id": "j-empty"}),
        httpx.Response(200, json={"status": "READY"}),
        httpx.Response(200, content=b""),
    ])
    engine = ShortGPTEngine(url="http://engine:9000", transport=fake)
    assets = _job_assets(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        engine.render(tmp_path / "final" / "empty.mp4", **assets)
    assert "empty" in str(exc.value).lower()


def test_moneyprinter_refuses_a_download_that_is_not_decodable_media(tmp_path):
    """A 200 with garbage bytes must never become the Job artifact (§9.5)."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-corrupt"}}),
        httpx.Response(200, json={"data": {"state": 1, "videos": ["tasks/j-corrupt/final-1.mp4"]}}),
        httpx.Response(200, content=b"not-a-video"),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "corrupt.mp4", **assets)

    assert exc.value.code == "upstream_result_corrupted"
    assert not (tmp_path / "final" / "corrupt.mp4").exists()
    # No post-step artefact is left behind either.
    assert not list((tmp_path / "final").glob("*-poststep.mp4"))


def test_an_interrupted_download_is_retried_under_the_same_submit_key(tmp_path, mp4_factory):
    """§5: обірваний download не реєструє артефакт і не вимагає нової генерації.

    Перерване завантаження — це транспортний збій уже прийнятої роботи. Attempt лишається
    без артефакту, а повтор іде з тим самим durable submit key, тому runtime відповідає
    з власного запису й upstream не отримує друге завдання для тієї самої генерації.
    """
    assets = _job_assets(tmp_path)
    key = "job-1-v1-interrupted"
    complete = mp4_factory(2.0)
    truncated = complete[: len(complete) // 3]
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-interrupted"}}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-interrupted/final-1.mp4"]}),
        # The runtime declares the finished length, but the transfer stopped early.
        httpx.Response(200, content=truncated,
                      headers={"content-length": str(len(complete))}),
        # The retry asks the runtime again and is answered from its durable record.
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "message": "reconciled",
                                  "data": {"task_id": "j-interrupted"}}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-interrupted/final-1.mp4"]}),
        httpx.Response(200, content=complete),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    output = tmp_path / "final" / "interrupted.mp4"

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(output, submit_key=key, **assets)

    assert refusal.value.code == "upstream_download_incomplete"
    assert not output.exists(), "обірваний download не стає артефактом"
    final_dir = tmp_path / "final"
    assert not list(final_dir.glob("*.tmp")), "тимчасовий файл не лишається"
    assert not list(final_dir.glob("*-poststep.mp4"))

    assert engine.render(output, submit_key=key, **assets) == output
    assert output.is_file() and output.stat().st_size > 0

    submits = [entry for entry in fake.requests if entry[1].endswith("/api/v1/videos")]
    assert len(submits) == 2
    assert all(entry[2]["headers"]["x-vertep-submit-key"] == key for entry in submits), \
        "обидва submit несуть той самий durable key — нова генерація неможлива"
    assert any(entry[1].endswith("/api/v1/videos/" + key) for entry in fake.requests) is False


def test_a_download_transferred_encoded_is_not_judged_by_its_wire_length(
    tmp_path, mp4_factory
):
    """The declared length of an encoded transfer describes the wire, not the artifact.

    A proxy may compress the result on the way out, so the bytes that arrive are not the
    bytes the runtime counted. Refusing such a result as interrupted would reject a
    complete artifact, so the comparison applies to an unencoded transfer only.
    """
    assets = _job_assets(tmp_path)
    complete = mp4_factory(2.0)
    encoded = gzip.compress(complete)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-encoded"}}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-encoded/final-1.mp4"]}),
        httpx.Response(200, content=encoded,
                       headers={"content-encoding": "gzip",
                                "content-length": str(len(encoded))}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    output = tmp_path / "final" / "encoded.mp4"

    assert engine.render(output, submit_key="job-1-v1-encoded", **assets) == output
    assert output.is_file() and output.stat().st_size > 0


def test_moneyprinter_bounds_polling_retries_and_refuses_the_task(tmp_path):
    """Transient upstream errors are retried a bounded number of times, then refused."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-flaky"}}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(200, json={"data": {"state": 1, "videos": ["tasks/j-flaky/final-1.mp4"]}}),
    ])
    engine = MoneyPrinterEngine(
        url="http://engine:8000", transport=fake,
        max_read_attempts=2, read_backoff=0.0,
    )

    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "flaky.mp4", **assets)

    assert exc.value.code == "upstream_transient_failure"
    # Snapshot, upload, voice and submit were never retried, only the read.
    assert [request[0] for request in fake.requests] == [
        "GET", "POST", "POST", "POST", "GET", "GET",
    ]
    assert not (tmp_path / "final" / "flaky.mp4").exists()


def test_moneyprinter_empty_download_rejected(tmp_path):
    """MoneyPrinterEngine rejects empty download (Issue #122 P2)."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"task_id": "j-empty"}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-empty/final-1.mp4"]}),
        httpx.Response(200, content=b""),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    with pytest.raises(RuntimeError) as exc:
        engine.render(tmp_path / "final" / "empty.mp4", **assets)
    assert "empty" in str(exc.value).lower()
    assert not (tmp_path / "final" / "empty.mp4").exists()


def test_remote_engine_cancel(tmp_path):
    fake = FakeTransport([
        httpx.Response(204),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assert engine.cancel("job-123") is True
    assert fake.requests[0][0] == "DELETE"
    assert fake.requests[0][1].endswith("/api/v1/tasks/job-123")


def test_remote_engine_cancel_treats_busy_task_as_not_cancelled(tmp_path):
    """A 409 from the pinned upstream means the task is still running."""
    fake = FakeTransport([
        httpx.Response(409, json={"status": 409, "message": "task is still running"}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assert engine.cancel("job-123") is False


def test_remote_engine_cancel_treats_unknown_task_as_not_cancelled(tmp_path):
    fake = FakeTransport([
        httpx.Response(404, json={"status": 404, "message": "task not found"}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assert engine.cancel("job-123") is False


# ---------------------------------------------------------------------------
# Durable submit key: one approved render, one upstream task (Issue #122 §5)
# ---------------------------------------------------------------------------


def _raises(error):
    """A queued FakeTransport response that fails with a transport error."""
    def _raise():
        raise error
    return _raise


def test_render_sends_the_durable_submit_key_and_uses_the_reconciled_task(
    tmp_path, mp4_factory
):
    """A lost submit response is resolved through the key, never resent blindly."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        _staged_voice(assets["audio"]),
        _raises(httpx.TimeoutException("lost response")),
        # Reconciliation of the key into the upstream task that already exists.
        httpx.Response(200, json={"status": 200, "state": "submitted",
                                  "data": {"task_id": "j-reconciled"}}),
        httpx.Response(200, json={"data": {"state": 1,
                                           "videos": ["tasks/j-reconciled/final-1.mp4"]}}),
        httpx.Response(200, content=mp4_factory(2.0)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    output = engine.render(tmp_path / "final" / "reconciled.mp4",
                           submit_key="job-1-v1-abc", **assets)

    assert output.stat().st_size > 0
    submit_call = fake.requests[3]
    assert submit_call[0] == "POST" and submit_call[1].endswith("/api/v1/videos")
    assert submit_call[2]["headers"]["x-vertep-submit-key"] == "job-1-v1-abc"
    # Exactly one submit, and it was resolved into the existing upstream task.
    assert [item for item in fake.requests
            if item[0] == "POST" and item[1].endswith("/api/v1/videos")] == [submit_call]
    reconcile = fake.requests[4]
    assert reconcile[0] == "GET"
    assert reconcile[1].endswith("/api/v1/videos/job-1-v1-abc")


def test_a_lost_submit_that_cannot_be_reconciled_is_terminal(tmp_path):
    """An unprovable submit is refused as unknown, never as a fresh attempt."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        _staged_voice(assets["audio"]),
        _raises(httpx.TimeoutException("lost response")),
        httpx.Response(200, json={"status": 200, "state": "unknown", "data": {}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "unknown.mp4",
                      submit_key="job-1-v1-abc", **assets)

    assert refusal.value.code == REASON_SUBMIT_UNKNOWN
    assert [item for item in fake.requests
            if item[0] == "POST" and item[1].endswith("/api/v1/videos")] != []
    # The submit was sent exactly once and never repeated.
    assert len([item for item in fake.requests
                if item[0] == "POST" and item[1].endswith("/api/v1/videos")]) == 1


def test_an_in_flight_submit_is_reconciled_instead_of_repeated(tmp_path):
    """A 409 for the same key resolves into the running attempt, not a new one."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(409, json={"status": 409, "reason": "upstream_submit_unknown"}),
        httpx.Response(200, json={"status": 200, "state": "submitted",
                                  "data": {"task_id": "j-inflight"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    engine._wait_for_status = lambda task_id: {
        "data": {"state": 1, "videos": [f"tasks/{task_id}/final-1.mp4"]},
    }

    with pytest.raises(Exception):
        # The render cannot finish without a download response, but the submit
        # itself must already have been resolved into the in-flight task.
        engine.render(tmp_path / "final" / "inflight.mp4",
                      submit_key="job-1-v1-abc", **assets)

    assert len([item for item in fake.requests
                if item[0] == "POST" and item[1].endswith("/api/v1/videos")]) == 1


def test_an_unresolvable_in_flight_submit_is_terminal(tmp_path):
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(409, json={"status": 409, "reason": "upstream_submit_unknown"}),
        httpx.Response(200, json={"status": 200, "state": "unknown", "data": {}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "inflight.mp4",
                      submit_key="job-1-v1-abc", **assets)

    assert refusal.value.code == REASON_SUBMIT_UNKNOWN


def test_cancel_addresses_the_durable_submit_key(tmp_path):
    """Cancellation uses the key, so it works without the upstream task id."""
    fake = FakeTransport([httpx.Response(204)])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    assert engine.cancel("tsk-1", submit_key="job-1-v1-abc") is True
    assert fake.requests[0][0] == "DELETE"
    assert fake.requests[0][1].endswith("/api/v1/videos/job-1-v1-abc")


def test_cancel_of_an_in_flight_submit_is_not_reported_as_cancelled(tmp_path):
    fake = FakeTransport([httpx.Response(409, json={"status": 409})])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)

    assert engine.cancel("tsk-1", submit_key="job-1-v1-abc") is False


def test_remote_engine_polling_transient_retry(tmp_path, mp4_factory):
    fake = FakeTransport([
        httpx.Response(200, json={"job_id": "j-retry"}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, json={"status": "READY"}),
        httpx.Response(200, content=mp4_factory(2.0)),
    ])
    engine = ShortGPTEngine(url="http://engine:9000", transport=fake, poll_interval=0.01)
    assets = _job_assets(tmp_path)
    output = engine.render(tmp_path / "final" / "retry.mp4", **assets)
    assert output.stat().st_size > 100


# ---------------------------------------------------------------------------
# Parity: native vs external on the same Job assets
# ---------------------------------------------------------------------------


def test_engine_parity_native_vs_external_on_same_job(tmp_path, mp4_factory):
    assets = _job_assets(tmp_path)

    # Native engine renders a real video from the Job assets.
    native_out = tmp_path / "native" / "video.mp4"
    NativeVertepEngine().render(native_out, **assets)
    assert native_out.stat().st_size > 100

    # External engine receives the exact same Job assets and produces the final
    # artifact at the requested output path (remote render + download).
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "uploaded-scene.png"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"task_id": "j-parity"}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-parity/final-1.mp4"]}),
        httpx.Response(200, content=mp4_factory(2.0)),
    ])
    external_out = tmp_path / "external" / "video.mp4"
    MoneyPrinterEngine(url="http://engine:8000", transport=fake).render(
        external_out, **assets
    )
    assert external_out.stat().st_size > 100

    # Parity: same render contract — both engines consumed the same assets
    # (images, durations, aspect ratio, task type) and produced the final output
    # path for the same Job. The submitted request is the fourth call: the first
    # reads the runtime snapshot, the second uploads the approved scene clip and
    # the third stages the approved voice.
    spec = fake.requests[3][2]["json"]
    # §9.3: the executor receives one pre-cut clip per scene, never the raw asset.
    assert [material["original_name"] for material in
            spec["video_materials"]] == ["scene-000.mp4"]
    assert spec["video_materials"][0]["url"] == "uploaded-scene.png"
    assert spec["video_materials"][0]["duration"] == assets["durations"][0]
    assert spec["custom_audio_file"].startswith("vertep-voice:")
    assert spec["video_aspect"] == assets["aspect_ratio"]
    assert spec["video_source"] == "local"


# ---------------------------------------------------------------------------
# Pinned MoneyPrinterTurbo bridge contract (Issue #122 P1)
# ---------------------------------------------------------------------------


def _contract_engine(tmp_path, responses=None):
    transport = FakeTransport(responses or [httpx.Response(200, json={"job_id": "j"})])
    return MoneyPrinterEngine(url="http://engine:8000", transport=transport), transport


def test_capabilities_are_callable_on_instance():
    money_printer = MoneyPrinterEngine(url="http://engine:8000")
    generic = ShortGPTEngine(url="http://engine:9000")

    caps = money_printer.capabilities()
    assert caps["contract_profile"] == "moneyprinter_v1"
    assert caps["upstream_reference"].startswith("harry0703/MoneyPrinterTurbo@")

    assert generic.capabilities()["contract_profile"] == "generic"
    with pytest.raises(TypeError):
        RemoteVideoEngine.capabilities()


def test_money_printer_capability_declaration():
    caps = MoneyPrinterEngine(url="http://engine:8000").capabilities()

    assert caps["endpoints"]["submit"] == "/api/v1/videos"
    assert caps["endpoints"]["status"] == "/api/v1/tasks/{task_id}"
    assert caps["endpoints"]["download"] == "/api/v1/download/{file_path}"
    assert caps["endpoints"]["upload_material"] == "/api/v1/video_materials"
    assert caps["status_mapping"] == {"-1": "failed", "1": "complete", "4": "processing"}
    assert caps["supported_aspect_ratios"] == ["16:9", "9:16"]
    assert caps["supported_task_types"] == ["image", "video"]
    assert "youtube" in caps["supported_presets"]
    assert caps["max_scene_duration_seconds"] == 15.0
    assert caps["requires_narration_audio"] is True
    assert caps["post_step_components"] == [
        "audio_mix",
        "subtitle_burn",
        "watermark_overlay",
        "preset_geometry",
    ]
    assert caps["fixed_submission_values"]["video_source"] == "local"
    assert caps["fixed_submission_values"]["subtitle_enabled"] is False
    assert "missing_voice_audio" in caps["pre_dispatch_rejections"]


def test_validate_contract_accepts_supported_plan(tmp_path):
    assets = _job_assets(tmp_path)
    assets["music"] = _wav(tmp_path / "music.wav")
    assets["subtitles"] = tmp_path / "voice.srt"
    assets["subtitles"].write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n", encoding="utf-8")
    assets["watermark"] = _png(tmp_path / "logo.png")
    assets["preset"] = "youtube"
    engine = MoneyPrinterEngine(url="http://engine:8000")

    plan = engine.validate_contract(**assets)

    assert plan["task_type"] == "image"
    assert plan["materials"] == assets["images"]
    assert plan["durations"] == [2.0]
    assert plan["narration"] == assets["audio"]
    assert plan["post_step"]["components"] == [
        "audio_mix",
        "subtitle_burn",
        "watermark_overlay",
        "preset_geometry",
    ]
    assert plan["post_step"]["audio_mix"]["music"] == str(assets["music"])
    assert plan["post_step"]["audio_mix"]["voice_filter"] == "loudnorm=I=-16:LRA=11:TP=-1.5"
    assert plan["post_step"]["audio_mix"]["music_volume"] == 0.15
    assert plan["post_step"]["subtitle_burn"]["policy"] == "BURN_SUBTITLES"


def test_validate_contract_skips_watermark_for_video_task(tmp_path):
    clip = _mp4(tmp_path / "scene.mp4")
    assets = {
        "images": None,
        "clips": [clip],
        "durations": [2.0],
        "audio": _wav(tmp_path / "voice.wav"),
        "music": None,
        "subtitles": None,
        "aspect_ratio": "16:9",
        "preset": None,
        "watermark": _png(tmp_path / "logo.png"),
        "task_type": "video",
    }
    plan = MoneyPrinterEngine(url="http://engine:8000").validate_contract(**assets)

    assert "watermark_overlay" not in plan["post_step"]
    assert plan["post_step"]["components"] == ["audio_mix"]


def _clip_unreadable(assets, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 32)
    assets.update(task_type="video", images=None, clips=[broken], durations=[2.0])


def _output_not_writable(assets, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    assets["output"] = blocker / "final" / "video.mp4"


CONTRACT_NEGATIVE_CASES = [
        ("schema_version_unsupported", lambda a, p: a.update(schema_version="v2")),
        ("unsupported_task_type", lambda a, p: a.update(task_type="audio")),
        ("no_materials", lambda a, p: a.update(images=[])),
        (
            "material_channel_mismatch",
            lambda a, p: a.update(clips=[p / "scene.mp4"]),
        ),
        (
            "asset_unreadable",
            lambda a, p: a.update(images=[p / "missing.png"]),
        ),
        (
            "unsupported_asset_format",
            lambda a, p: a.update(images=[_image(p / "scene.ppm")]),
        ),
        (
            "asset_below_min_resolution",
            lambda a, p: a.update(images=[_png(p / "small.png", 64, 64)]),
        ),
        ("clip_unreadable", _clip_unreadable),
        ("duration_count_mismatch", lambda a, p: a.update(durations=[1.0, 2.0])),
        ("invalid_duration", lambda a, p: a.update(durations=[0.0])),
        ("invalid_duration", lambda a, p: a.update(durations=[-1.0])),
        ("invalid_duration", lambda a, p: a.update(durations=[float("nan")])),
        ("invalid_duration", lambda a, p: a.update(durations=["x"])),
        ("duration_mismatch", lambda a, p: a.update(durations=[9.0])),
        ("duration_exceeds_clip_limit", lambda a, p: a.update(durations=[16.0])),
        ("missing_voice_audio", lambda a, p: a.update(audio=None)),
        (
            "voice_audio_unusable",
            lambda a, p: a.update(audio=p / "missing.wav"),
        ),
        ("music_unusable", lambda a, p: a.update(music=p / "missing.wav")),
        (
            "subtitle_unusable",
            lambda a, p: a.update(subtitles=p / "missing.srt"),
        ),
        (
            "subtitle_unusable",
            lambda a, p: a.update(subtitles=_write(p / "voice.vtt", "WEBVTT\n")),
        ),
        ("unsupported_aspect", lambda a, p: a.update(aspect_ratio="1:1")),
        ("preset_unknown", lambda a, p: a.update(preset="cinema-4k")),
        ("watermark_unusable", lambda a, p: a.update(watermark=p / "missing.png")),
        ("output_not_writable", _output_not_writable),
]


def test_every_declared_rejection_code_has_a_negative_case():
    declared = set(MONEY_PRINTER_CONTRACT.pre_dispatch_rejections)
    covered = {code for code, _ in CONTRACT_NEGATIVE_CASES}

    assert declared - covered == set()
    assert covered - declared == set()


@pytest.mark.parametrize("code,mutate", CONTRACT_NEGATIVE_CASES)
def test_validate_contract_rejects_before_dispatch(code, mutate, tmp_path):
    assets = _job_assets(tmp_path)
    mutate(assets, tmp_path)
    engine, transport = _contract_engine(tmp_path)

    with pytest.raises(BridgeContractError) as exc:
        engine.validate_contract(**assets)

    assert exc.value.code == code
    assert transport.requests == []

    if "schema_version" not in assets:
        destination = assets.pop("output", tmp_path / "final" / "v.mp4")
        with pytest.raises(BridgeContractError):
            engine.render(destination, **assets)
        assert transport.requests == []


def _write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def test_render_rejects_contract_violation_without_http_call(tmp_path):
    assets = _job_assets(tmp_path)
    assets["durations"] = [-1.0]
    engine, transport = _contract_engine(tmp_path)

    with pytest.raises(BridgeContractError) as exc:
        engine.render(tmp_path / "final" / "v.mp4", **assets)

    assert exc.value.code == "invalid_duration"
    assert transport.requests == []


def test_render_rejects_unwritable_output_without_http_call(tmp_path):
    assets = _job_assets(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    engine, transport = _contract_engine(tmp_path)

    with pytest.raises(BridgeContractError) as exc:
        engine.render(blocker / "final" / "v.mp4", **assets)

    assert exc.value.code == "output_not_writable"
    assert transport.requests == []


def test_build_upstream_request_matches_pinned_schema(tmp_path):
    engine = MoneyPrinterEngine(url="http://engine:8000")
    payload = engine.build_upstream_request(
        subject="Щоденне змагання",
        script="Сцена перша.\nСцена друга.",
        materials=[{"provider": "local", "url": "a.mp4", "duration": 4}],
        narration_path="tasks/t1/voice.wav",
        aspect_ratio="9:16",
        clip_duration=5,
    )

    assert payload["video_subject"] == "Щоденне змагання"
    assert payload["video_script"] == "Сцена перша.\nСцена друга."
    assert payload["video_source"] == "local"
    assert payload["video_materials"] == [
        {"provider": "local", "url": "a.mp4", "duration": 4}
    ]
    assert payload["custom_audio_file"] == "tasks/t1/voice.wav"
    assert payload["video_aspect"] == "9:16"
    assert payload["video_fit_mode"] == "contain"
    assert payload["video_concat_mode"] == "sequential"
    assert payload["video_transition_mode"] is None
    assert payload["video_clip_duration"] == 5
    assert payload["video_count"] == 1
    assert payload["bgm_type"] == ""
    assert payload["subtitle_enabled"] is False
    assert payload["n_threads"] == 2
    assert set(payload) == set(MONEY_PRINTER_CONTRACT.submission_parameters) | {
        "video_terms",
        "bgm_file",
        "bgm_volume",
        "video_language",
        "n_threads",
    }


@pytest.mark.parametrize(
    "code,kwargs",
    [
        ("script_unavailable", {"script": "   "}),
        ("unsupported_aspect", {"aspect_ratio": "4:3"}),
        ("invalid_clip_duration", {"clip_duration": 0}),
        ("invalid_clip_duration", {"clip_duration": 16}),
        ("invalid_clip_duration", {"clip_duration": 5.5}),
        ("invalid_clip_duration", {"clip_duration": True}),
    ],
)
def test_build_upstream_request_rejects_invalid_payloads(code, kwargs, tmp_path):
    engine = MoneyPrinterEngine(url="http://engine:8000")
    payload = {
        "subject": "s",
        "script": "text",
        "materials": [],
        "narration_path": "tasks/t1/voice.wav",
        "aspect_ratio": "16:9",
        "clip_duration": 5,
    }
    payload.update(kwargs)

    with pytest.raises(BridgeContractError) as exc:
        engine.build_upstream_request(**payload)
    assert exc.value.code == code


def test_build_upstream_request_requires_pinned_profile():
    with pytest.raises(BridgeContractError) as exc:
        ShortGPTEngine(url="http://engine:9000").build_upstream_request(
            subject="s",
            script="text",
            materials=[],
            narration_path="voice.wav",
            aspect_ratio="16:9",
            clip_duration=5,
        )
    assert exc.value.code == "upstream_mapping_unavailable"


def test_map_upstream_status_states():
    engine = MoneyPrinterEngine(url="http://engine:8000")

    processing = engine.map_upstream_status(
        {"task_id": "t1", "state": 4, "progress": 30}
    )
    assert processing["state"] == "processing"
    assert processing["outputs"] == []

    complete = engine.map_upstream_status(
        {"task_id": "t1", "state": 1, "progress": 100, "videos": ["tasks/t1/final-1.mp4"]}
    )
    assert complete["state"] == "complete"
    assert complete["outputs"] == ["tasks/t1/final-1.mp4"]

    failed = engine.map_upstream_status(
        {"task_id": "t1", "state": -1, "failed_stage": "audio", "error": "TTS timeout"}
    )
    assert failed["state"] == "failed"
    assert failed["failed_stage"] == "audio"
    assert failed["error"] == "TTS timeout"

    assert engine.map_upstream_status({"task_id": "t1", "state": 9})["state"] == "unknown"


@pytest.mark.parametrize(
    "code,payload",
    [
        ("upstream_status_malformed", "not-an-object"),
        ("upstream_status_malformed", {}),
        ("upstream_status_malformed", {"state": "4"}),
        ("upstream_status_malformed", {"state": True}),
        ("upstream_result_missing_output", {"state": 1}),
        ("upstream_result_missing_output", {"state": 1, "videos": []}),
        ("upstream_result_missing_output", {"state": 1, "videos": [None]}),
    ],
)
def test_map_upstream_status_rejects_malformed(code, payload):
    engine = MoneyPrinterEngine(url="http://engine:8000")
    with pytest.raises(BridgeContractError) as exc:
        engine.map_upstream_status(payload)
    assert exc.value.code == code


def test_parse_output_reference_normalises_task_scoped_paths():
    parse = RemoteVideoEngine.parse_output_reference

    # The upstream ``_task_file_to_uri`` prefixes with ``tasks/``, but the download
    # endpoint resolves relative to the task directory and expects
    # ``{task_id}/{filename}`` (no ``tasks/`` prefix). The parser strips it.
    assert parse("tasks/t1/final-1.mp4") == "t1/final-1.mp4"
    assert parse("/tasks/t1/final-1.mp4") == "t1/final-1.mp4"
    assert (
        parse("https://engine:8000/tasks/t1/final-1.mp4") == "t1/final-1.mp4"
    )

    for unsafe in ("", "   ", "../etc/passwd", "/etc/passwd", "final-1.mp4"):
        with pytest.raises(BridgeContractError) as exc:
            parse(unsafe)
        assert exc.value.code in {
            "upstream_output_path_unsafe",
            "upstream_result_missing_output",
        }


def test_generic_profile_keeps_legacy_render_contract(tmp_path, mp4_factory):
    clip = tmp_path / "scene.mp4"
    clip.write_bytes(b"\x00" * 32)
    assets = {
        "images": None,
        "clips": [clip],
        "durations": [],
        "audio": None,
        "music": None,
        "subtitles": None,
        "aspect_ratio": "16:9",
        "preset": None,
        "watermark": None,
        "task_type": "video",
    }
    fake = FakeTransport([
        httpx.Response(200, json={"job_id": "j-generic"}),
        httpx.Response(200, json={"status": "READY"}),
        httpx.Response(200, content=mp4_factory(2.0)),
    ])
    output = ShortGPTEngine(url="http://engine:9000", transport=fake).render(
        tmp_path / "final" / "v.mp4", **assets
    )

    assert output.stat().st_size > 100
    assert fake.requests[-1][1].endswith("/download/j-generic")


# ---------------------------------------------------------------------------
# Registry swappability
# ---------------------------------------------------------------------------


def test_registry_video_engine_is_swappable():
    reg = get_providers()
    reg.replace("video_engine", MoneyPrinterEngine(url="http://engine:8000"))
    assert isinstance(reg.video_engine(), MoneyPrinterEngine)
    assert isinstance(DefaultAssemblyProvider(object()), object)


# ---------------------------------------------------------------------------
# Issue #122 P2: Material upload wrapper
# ---------------------------------------------------------------------------


def test_material_upload_sends_files_with_sha256(tmp_path):
    """Material upload wrapper sends files and records SHA256 (Issue #122 P2).

    The response is the real pinned-upstream envelope
    ``{"status": 200, "message": "success", "data": {"file": <storage key>}}``
    (app/controllers/v1/video.py:upload_video_material_file), not a flat
    filename object.
    """
    fake = FakeTransport([
        httpx.Response(
            200,
            json={
                "status": 200,
                "message": "success",
                "data": {"file": "8f14e45fceea167a5a36dedd4bea2543-scene.png"},
            },
        ),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    image = _png(tmp_path / "scene.png")

    uploaded = engine._upload_materials(
        materials=[image],
        task_id="test-task",
    )

    assert len(uploaded) == 1
    assert uploaded[0]["original_name"] == "scene.png"
    assert "sha256" in uploaded[0]
    assert uploaded[0]["url"] == "8f14e45fceea167a5a36dedd4bea2543-scene.png"
    assert uploaded[0]["provider"] == "local"
    assert len(fake.requests) == 1
    assert fake.requests[0][1].endswith("/api/v1/video_materials")
    # The wrapper is the only reachable endpoint and builds the multipart envelope,
    # so the clip travels as raw bytes with its name in a header.
    assert fake.requests[0][2]["content"] == image.read_bytes()
    assert fake.requests[0][2]["headers"]["x-vertep-filename"] == "scene.png"
    assert "files" not in fake.requests[0][2]


def test_material_upload_reads_the_real_upstream_envelope(tmp_path):
    """Regression: the pinned API returns ``data.file``.

    Reading ``data.filename`` (or a top-level ``filename``) made every
    real upload fail with ``upstream_upload_failed`` even though the
    upstream had accepted the file.
    """
    fake = FakeTransport([
        httpx.Response(200, json={"status": 200, "data": {"file": "material-1.mp4"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    image = _png(tmp_path / "scene.png")

    uploaded = engine._upload_materials(materials=[image], task_id="test-task")

    assert uploaded[0]["url"] == "material-1.mp4"


def test_material_upload_rejects_wrong_envelope_key(tmp_path):
    """A response that does not carry ``data.file`` is rejected."""
    fake = FakeTransport([
        httpx.Response(200, json={"status": 200, "data": {"filename": "scene.png"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    image = _png(tmp_path / "scene.png")

    with pytest.raises(BridgeContractError) as exc:
        engine._upload_materials(materials=[image], task_id="test-task")
    assert exc.value.code == "upstream_upload_failed"


def test_material_upload_fails_on_unreadable_file(tmp_path):
    """Material upload rejects unreadable files before submit (Issue #122 P2)."""
    fake = FakeTransport([])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    unreadable = tmp_path / "missing.png"

    with pytest.raises(BridgeContractError) as exc:
        engine._upload_materials(materials=[unreadable], task_id="test-task")
    assert exc.value.code == "asset_unreadable"


def test_material_upload_fails_on_upstream_error(tmp_path):
    """Material upload fails on upstream error before submit (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(500, json={"error": "storage full"}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    image = _png(tmp_path / "scene.png")

    with pytest.raises(BridgeContractError) as exc:
        engine._upload_materials(materials=[image], task_id="test-task")
    assert exc.value.code == "upstream_upload_failed"


# ---------------------------------------------------------------------------
# Issue #122 P2: Health check and readiness
# ---------------------------------------------------------------------------


def _wrapper_ready_body(**overrides):
    """A wrapper ``/health`` report that satisfies every §9.12 readiness gate."""
    checks = {
        "snapshot": {
            "upstream_commit": MONEY_PRINTER_CONTRACT.upstream_reference.split("@")[-1],
            "upstream_reference": MONEY_PRINTER_CONTRACT.upstream_reference,
            "upstream_version": "1.3.7",
            "bridge_version": "v1",
            "bridge_schema_version": "v1",
            "image_digest": "sha256:" + "a" * 64,
            "dependency_count": 2,
            "inventory_digest": "b" * 64,
        },
        "upstream_auth_enforced": True,
        # Built from the contract, so a widened §9.12 field list cannot leave this
        # "ready" report proving less than the engine requires.
        "submit_schema": sorted(FIXED_SUBMIT_FIELDS),
    }
    checks.update(overrides)
    return {"service": "moneyprinter", "status": "ready", "checks": checks}


def _health_from(body, status_code=200):
    fake = FakeTransport([httpx.Response(status_code, json=body)])
    return MoneyPrinterEngine(url="http://engine:8098", transport=fake).health_check()


def test_health_check_fails_without_url():
    """Health check fails when URL not configured (Issue #122 P2)."""
    engine = MoneyPrinterEngine(url="")
    health = engine.health_check()

    assert health["available"] is False
    assert health["endpoint_reachable"] is False
    assert health["reason"] == "engine_not_configured"
    assert "MONEY_PRINTER_URL" in health["error"]


def test_health_check_reports_endpoint_reachable():
    """Readiness is proven by the wrapper, on the real executor (Issue #122 §9.12)."""
    health = _health_from(_wrapper_ready_body())

    assert health["available"] is True
    assert health["endpoint_reachable"] is True
    assert health["auth_enforced"] is True
    assert health["schema_compatible"] is True
    assert health["snapshot_verified"] is True
    assert health["reason"] is None
    assert health["error"] is None


def test_health_check_reports_schema_compatibility():
    """Schema compatibility comes from the upstream submit schema, not from a dict."""
    health = _health_from(_wrapper_ready_body())

    assert health["schema_compatible"] is True


def test_health_check_refuses_an_unauthenticated_upstream():
    """HTTP 401 from the runtime must not read as ready (Issue #122 P2)."""
    health = _health_from({
        "service": "moneyprinter",
        "status": "unavailable",
        "checks": {
            "reason": "upstream_unauthenticated",
            "detail": "pinned upstream API does not enforce x-api-key authentication",
        },
    }, status_code=503)

    assert health["available"] is False
    assert health["reason"] == "upstream_unauthenticated"
    assert "x-api-key" in health["error"]


def test_health_check_refuses_a_runtime_without_inventory():
    """A runtime that publishes no inventory is refused, not assumed ready."""
    health = _health_from(_wrapper_ready_body(snapshot=None))

    assert health["available"] is False
    assert health["reason"] == "runtime_inventory_unverified"
    assert health["snapshot_verified"] is False


def test_health_check_refuses_an_arbitrary_response_as_schema():
    """An arbitrary dict is not a proof of schema compatibility."""
    health = _health_from(_wrapper_ready_body(submit_schema={"unexpected": "shape"}))

    assert health["available"] is False
    assert health["reason"] == "upstream_schema_unsupported"
    assert health["schema_compatible"] is False


def test_health_check_refuses_when_auth_is_not_proven():
    """Reachability without an auth proof is not readiness."""
    health = _health_from(_wrapper_ready_body(upstream_auth_enforced=False))

    assert health["available"] is False
    assert health["endpoint_reachable"] is False


def test_health_check_refuses_a_missing_submit_schema():
    health = _health_from(_wrapper_ready_body(submit_schema=None))

    assert health["available"] is False
    assert health["reason"] == "upstream_schema_unsupported"


def test_health_check_refuses_a_runtime_that_proved_only_the_starting_fields():
    """Issue #122 P4: proving the fields that start a task is not proving the contract.

    The pinned request model ignores unknown keys, so a runtime that stopped declaring a
    compose field would accept the submit and build the render with its own default. The
    engine has to call that incompatible instead of reporting the fixed §9.4 set honoured.
    """
    from adapters.providers.base import REQUIRED_SUBMIT_FIELDS

    health = _health_from(_wrapper_ready_body(
        submit_schema=sorted(REQUIRED_SUBMIT_FIELDS)))

    assert health["available"] is False
    assert health["schema_compatible"] is False
    assert health["reason"] == "upstream_schema_unsupported"
    assert "video_fit_mode" in health["error"]


def test_health_check_refuses_runtime_commit_drift():
    health = _health_from(_wrapper_ready_body(snapshot={
        "upstream_commit": "0" * 40,
        "bridge_version": "v1",
        "bridge_schema_version": "v1",
        "image_digest": "sha256:" + "a" * 64,
    }))

    assert health["available"] is False
    assert health["reason"] == "runtime_inventory_unverified"
    assert "commit drift" in health["error"]


def test_health_check_refuses_a_mutable_image_tag():
    health = _health_from(_wrapper_ready_body(snapshot={
        "upstream_commit": MONEY_PRINTER_CONTRACT.upstream_reference.split("@")[-1],
        "bridge_version": "v1",
        "bridge_schema_version": "v1",
        "image_digest": "moneyprinter:1.3.7",
    }))

    assert health["available"] is False
    assert health["reason"] == "engine_snapshot_mismatch"


def test_health_check_refuses_bridge_schema_drift():
    health = _health_from(_wrapper_ready_body(snapshot={
        "upstream_commit": MONEY_PRINTER_CONTRACT.upstream_reference.split("@")[-1],
        "bridge_version": "v1",
        "bridge_schema_version": "v2",
        "image_digest": "sha256:" + "a" * 64,
    }))

    assert health["available"] is False
    assert health["reason"] == "engine_snapshot_mismatch"


def test_health_check_refuses_an_unreachable_wrapper():
    fake = FakeTransport([httpx.ConnectError("connection refused")])
    engine = MoneyPrinterEngine(url="http://engine:8098", transport=fake)

    health = engine.health_check()

    assert health["available"] is False
    assert health["reason"] == "wrapper_unreachable"


def test_health_check_includes_pinned_upstream_reference():
    """Health check includes pinned upstream reference (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"data": []}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    health = engine.health_check()

    assert health["pinned_upstream"] == MONEY_PRINTER_CONTRACT.upstream_reference


def test_health_check_reports_snapshot_verification():
    """A verified snapshot is reported back to the caller."""
    health = _health_from(_wrapper_ready_body())

    assert health["runtime_manifest"] is not None
    assert health["snapshot_verified"] is True
    assert health["image_digest"] == "sha256:" + "a" * 64


def test_capabilities_includes_health_check():
    """Capabilities endpoint includes health check for MoneyPrinterEngine (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json=_wrapper_ready_body()),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8098", transport=fake)
    caps = engine.capabilities()

    assert "health" in caps
    assert caps["health"]["available"] is True
    assert caps["ready"] is True


def test_capabilities_are_not_ready_without_a_proof():
    """A refused runtime must not be advertised as ready to dispatch."""
    fake = FakeTransport([
        httpx.Response(503, json={
            "service": "moneyprinter",
            "status": "unavailable",
            "checks": {"reason": "upstream_schema_unsupported"},
        }),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8098", transport=fake)
    caps = engine.capabilities()

    assert caps["health"]["available"] is False
    assert caps["ready"] is False
    assert caps["health"]["reason"] == "upstream_schema_unsupported"


# ---------------------------------------------------------------------------
# Issue #122 P2: Runtime manifest
# ---------------------------------------------------------------------------


def test_load_manifest_parses_inventory(tmp_path):
    """Runtime manifest loads and parses inventory file (Issue #122 P2)."""
    manifest_file = tmp_path / "runtime-inventory.json"
    manifest_data = {
        "format": "moneyprinter_runtime/v1",
        "bridge_version": "v1",
        "bridge_schema_version": "v1",
        "components": {
            "moneyprinter": {
                "commit": "2e1b30396e059e55939cc802c60faac2061e4d41",
                "repository": "harry0703/MoneyPrinterTurbo"
            }
        },
        "dependencies": ["torch==2.0.0", "numpy==1.24.0"]
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_manifest(manifest_file)

    assert manifest.format == "moneyprinter_runtime/v1"
    assert manifest.bridge_version == "v1"
    assert manifest.upstream_commit == "2e1b30396e059e55939cc802c60faac2061e4d41"
    assert manifest.upstream_repository == "harry0703/MoneyPrinterTurbo"
    assert len(manifest.dependencies) == 2


def test_manifest_compute_digest(tmp_path):
    """Manifest computes SHA256 digest for verification (Issue #122 P2)."""
    manifest_file = tmp_path / "runtime-inventory.json"
    manifest_data = {
        "format": "moneyprinter_runtime/v1",
        "bridge_version": "v1",
        "bridge_schema_version": "v1",
        "components": {},
        "dependencies": []
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_manifest(manifest_file)
    digest = manifest.compute_digest()
    assert isinstance(digest, str)
    assert len(digest) == 64


def test_manifest_verify_commit(tmp_path):
    """Manifest verification checks commit match (Issue #122 P2)."""
    manifest_file = tmp_path / "runtime-inventory.json"
    manifest_data = {
        "format": "moneyprinter_runtime/v1",
        "bridge_version": "v1",
        "bridge_schema_version": "v1",
        "components": {
            "moneyprinter": {
                "commit": "2e1b30396e059e55939cc802c60faac2061e4d41"
            }
        },
        "dependencies": []
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_manifest(manifest_file)
    assert manifest.verify(expected_commit="2e1b30396e059e55939cc802c60faac2061e4d41") is True
    assert manifest.verify(expected_commit="wrong-commit") is False


def test_generate_sbom_from_manifest(tmp_path):
    """SBOM generation from manifest (Issue #122 P2)."""
    manifest_file = tmp_path / "runtime-inventory.json"
    manifest_data = {
        "format": "moneyprinter_runtime/v1",
        "bridge_version": "v1",
        "bridge_schema_version": "v1",
        "components": {
            "moneyprinter": {
                "version": "main",
                "commit": "2e1b30396e059e55939cc802c60faac2061e4d41",
                "repository": "harry0703/MoneyPrinterTurbo"
            }
        },
        "dependencies": ["torch==2.0.0"]
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_manifest(manifest_file)
    sbom = generate_sbom(manifest)

    assert sbom["format"] == "sbom/v1"
    assert sbom["runtime_format"] == "moneyprinter_runtime/v1"
    assert len(sbom["components"]) == 1
    assert sbom["components"][0]["name"] == "moneyprinter"
    assert sbom["components"][0]["commit"] == "2e1b30396e059e55939cc802c60faac2061e4d41"
    assert "digest" in sbom
    assert len(sbom["dependencies"]) == 1

# ---------------------------------------------------------------------------
# P6 §5: the release of a cancelled attempt comes from the runtime, not from CORE
# ---------------------------------------------------------------------------


class _StatusTransport(FakeTransport):
    """Transport that answers the submit-status route and refuses the abort."""

    def __init__(self, runtime_state, *, abort_status: int = 409, status_status: int = 200):
        super().__init__([])
        self.runtime_state = runtime_state
        self.abort_status = abort_status
        self.status_status = status_status

    def delete(self, url, headers=None, timeout=None):
        self.requests.append(("DELETE", url))
        return httpx.Response(self.abort_status, json={"status": self.abort_status})

    def get(self, url, headers=None, timeout=None):
        self.requests.append(("GET", url))
        if self.status_status != 200:
            return httpx.Response(self.status_status, json={"status": self.status_status})
        return httpx.Response(200, json={
            "status": 200, "state": "submitted", "runtime_state": self.runtime_state,
            "data": {"task_id": "j-1"},
        })


def _external(status_transport) -> MoneyPrinterEngine:
    return MoneyPrinterEngine(url="http://engine:8000", transport=status_transport)


@pytest.mark.parametrize("runtime_state", ["finished", "failed", "absent"])
def test_a_runtime_that_no_longer_runs_the_attempt_is_released(runtime_state):
    engine = _external(_StatusTransport(runtime_state))

    assert engine.release_state("job-1-v1-abc") == RELEASE_RELEASED


def test_a_still_running_runtime_is_never_released():
    """A refused abort plus a running task is the exact case §9.8 describes."""
    engine = _external(_StatusTransport("running"))

    assert engine.release_state("job-1-v1-abc") == RELEASE_UNCONFIRMED


@pytest.mark.parametrize("status_status", [500, 503])
def test_an_unanswering_runtime_is_never_released(status_status):
    engine = _external(_StatusTransport("running", status_status=status_status))

    assert engine.release_state("job-1-v1-abc") == RELEASE_UNCONFIRMED


def test_an_unknown_submit_key_at_the_runtime_is_a_release():
    """The runtime no longer holds the key, so nothing of that attempt is running."""
    engine = _external(_StatusTransport("running", status_status=404))

    assert engine.release_state("job-1-v1-abc") == RELEASE_RELEASED


def test_a_transport_failure_is_never_a_release():
    class _BrokenTransport(FakeTransport):
        def get(self, url, headers=None, timeout=None):
            raise httpx.ConnectError("runtime unreachable")

    assert _external(_BrokenTransport([])).release_state("job-1-v1-abc") == RELEASE_UNCONFIRMED


def test_an_engine_without_a_key_or_a_route_never_releases():
    assert MoneyPrinterEngine(url="http://engine:8000", transport=FakeTransport([])) \
        .release_state(None) == RELEASE_UNCONFIRMED
    assert _external(_StatusTransport("absent")).release_state("") == RELEASE_UNCONFIRMED


def test_a_local_render_holds_no_remote_runtime():
    """Native has no pinned upstream, so there is no remote lease to keep or prove."""
    assert NativeVertepEngine().release_state("job-1-v1-abc") == RELEASE_NOT_APPLIED


def test_a_task_the_upstream_no_longer_knows_reports_absent(tmp_path):
    """§5: a runtime that answers 404 holds no task, and the attempt says so.

    An unknown task is not a transient read failure. The attempt must end
    terminally with the absent reason, register no artifact, and leave the runtime
    provably free — never be retried as a degraded upstream or reported as success.
    """
    assets = _job_assets(tmp_path)
    key = "job-1-v1-absent"
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-absent"}}),
        *[httpx.Response(404, json={"status": 404, "message": "task not found"})
          for _ in range(2)],
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake,
                                poll_interval=0.0, read_backoff=0.0,
                                max_read_attempts=2)
    output = tmp_path / "final" / "absent.mp4"

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(output, submit_key=key, **assets)

    assert refusal.value.code == REASON_TASK_ABSENT
    assert "404" in str(refusal.value), "відмова має назвати те, що відповів upstream"
    assert not output.exists(), "відсутній task не реєструє артефакт"
    assert [request[0] for request in fake.requests] == [
        "GET", "POST", "POST", "POST", "GET", "GET",
    ], "snapshot, upload, voice і submit не повторюються — повторюється лише читання"

    # The runtime no longer holds the key either, so nothing of the attempt runs.
    assert _external(_StatusTransport("running", status_status=404)).release_state(key) \
        == RELEASE_RELEASED


def test_an_unreachable_upstream_is_unreachable_and_never_absent(tmp_path):
    """§5: an upstream that cannot be reached proves nothing about its state.

    The render must end terminally as unreachable rather than hang, and the release
    stays unconfirmed: a silent runtime is never read as an absent task or a free one,
    so CORE keeps it out of the next render.
    """
    assets = _job_assets(tmp_path)

    class _Unreachable(FakeTransport):
        def get(self, url, **kwargs):
            if "/api/v1/tasks/j-dark" in url:
                self.requests.append(("GET", url, dict(kwargs)))
                raise httpx.ConnectError("runtime unreachable")
            return super().get(url, **kwargs)

    fake = _Unreachable([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-dark"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake,
                                poll_interval=0.0, read_backoff=0.0,
                                max_read_attempts=2)
    output = tmp_path / "final" / "dark.mp4"

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(output, submit_key="job-1-v1-dark", **assets)

    assert refusal.value.code == REASON_UPSTREAM_UNREACHABLE
    assert not output.exists(), "недосяжний upstream не реєструє артефакт"
    status_calls = [request for request in fake.requests if request[0] == "GET"
                    and "/api/v1/tasks/j-dark" in request[1]]
    assert len(status_calls) == 2, "опитування обмежене, а не нескінченне"

    class _Silent(_StatusTransport):
        def get(self, url, headers=None, timeout=None):
            raise httpx.ConnectError("runtime unreachable")

    assert _external(_Silent("absent")).release_state("job-1-v1-dark") \
        == RELEASE_UNCONFIRMED


def test_a_rate_limited_status_is_retried_within_a_bound_and_ends_terminally(tmp_path):
    """§5: 429 is retried a bounded number of times, then refused with its cause."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-limited"}}),
        *[httpx.Response(429, json={"status": 429, "message": "slow down"})
          for _ in range(6)],
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake,
                                poll_interval=0.0, timeout=60.0,
                                max_read_attempts=2, read_backoff=0.0)

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "limited.mp4", **assets)

    assert refusal.value.code == "upstream_transient_failure"
    assert "429" in str(refusal.value), "the refusal must name what the upstream answered"
    status_calls = [request for request in fake.requests if request[0] == "GET"
                    and "/api/v1/tasks/j-limited" in request[1]]
    assert len(status_calls) == 2, \
        f"a rate limit must be retried exactly within the bound, made {len(status_calls)}"
    assert not (tmp_path / "final" / "limited.mp4").exists()


def test_a_malformed_status_body_is_a_clear_terminal_failure(tmp_path):
    """§5: a malformed response fails with a contract reason, not a decoder crash."""
    assets = _job_assets(tmp_path)
    fake = FakeTransport([
        httpx.Response(200, json=_runtime_body()),
        httpx.Response(200, json={"status": 200, "data": {"file": "scene-000.mp4"}}),
        _staged_voice(assets["audio"]),
        httpx.Response(200, json={"status": 200, "data": {"task_id": "j-garbage"}}),
        *[httpx.Response(200, text="<html>not json</html>") for _ in range(4)],
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake,
                                poll_interval=0.0, timeout=60.0,
                                max_read_attempts=1, read_backoff=0.0)

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "garbage.mp4", **assets)

    assert refusal.value.code == REASON_SCHEMA_UNSUPPORTED
    assert not (tmp_path / "final" / "garbage.mp4").exists(), \
        "an unmappable status must never be registered as a finished render"
