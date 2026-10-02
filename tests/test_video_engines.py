from adapters.providers.base import BridgeContractError
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

import json
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
    }


# ---------------------------------------------------------------------------
# Factory: opt-in + fallback
# ---------------------------------------------------------------------------


def test_video_engine_default_is_native(monkeypatch):
    monkeypatch.delenv("VERTEP_VIDEO_ENGINE", raising=False)
    engine = _make_video_engine()
    assert isinstance(engine, NativeVertepEngine)


def test_video_engine_falls_back_when_external_not_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "")
    assert isinstance(_make_video_engine(), NativeVertepEngine)


def test_video_engine_enables_money_printer_when_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://engine:8000")
    assert isinstance(_make_video_engine(), MoneyPrinterEngine)


def test_video_engine_enables_shortgpt_when_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "shortgpt")
    monkeypatch.setenv("SHORTGPT_URL", "http://engine:9000")
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


def test_remote_engine_http_flow(tmp_path):
    """Test remote engine HTTP flow with material upload (Issue #122 P2)."""
    fake = FakeTransport([
        # Material upload response
        httpx.Response(200, json={"filename": "uploaded-scene.png"}),
        # Submit task response
        httpx.Response(200, json={"task_id": "j1"}),
        # Status check response (for MoneyPrinterEngine - map_upstream_status)
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j1/final-1.mp4"]}),
        # Download response
        httpx.Response(200, content=b"mp4-bytes"),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assets = _job_assets(tmp_path)
    output = engine.render(tmp_path / "final" / "external.mp4", **assets)

    assert output.read_bytes() == b"mp4-bytes"
    methods = [r[0] for r in fake.requests]
    # POST upload, POST submit, GET status, GET download
    assert methods == ["POST", "POST", "GET", "GET"]
    assert fake.requests[0][1].endswith("/api/v1/video_materials")
    assert fake.requests[1][1].endswith("/api/v1/videos")
    assert fake.requests[2][1].endswith("/api/v1/tasks/j1")
    # Download endpoint uses the contract template
    assert "/api/v1/download/" in fake.requests[3][1]


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


def test_moneyprinter_empty_download_rejected(tmp_path):
    """MoneyPrinterEngine rejects empty download (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"filename": "uploaded-scene.png"}),
        httpx.Response(200, json={"task_id": "j-empty"}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-empty/final-1.mp4"]}),
        httpx.Response(200, content=b""),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assets = _job_assets(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        engine.render(tmp_path / "final" / "empty.mp4", **assets)
    assert "empty" in str(exc.value).lower()


def test_remote_engine_cancel(tmp_path):
    fake = FakeTransport([
        httpx.Response(204),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assert engine.cancel("job-123") is True
    assert fake.requests[0][0] == "DELETE"
    assert fake.requests[0][1].endswith("/jobs/job-123")


def test_remote_engine_polling_transient_retry(tmp_path):
    """ShortGPTEngine (legacy flow) retries transient polling errors (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"job_id": "j-retry"}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, json={"status": "READY"}),
        httpx.Response(200, content=b"retry-success"),
    ])
    engine = ShortGPTEngine(url="http://engine:9000", transport=fake, poll_interval=0.01)
    assets = _job_assets(tmp_path)
    output = engine.render(tmp_path / "final" / "retry.mp4", **assets)
    assert output.read_bytes() == b"retry-success"


# ---------------------------------------------------------------------------
# Parity: native vs external on the same Job assets
# ---------------------------------------------------------------------------


def test_engine_parity_native_vs_external_on_same_job(tmp_path):
    assets = _job_assets(tmp_path)

    # Native engine renders a real video from the Job assets.
    native_out = tmp_path / "native" / "video.mp4"
    NativeVertepEngine().render(native_out, **assets)
    assert native_out.stat().st_size > 100

    # External engine receives the exact same Job assets and produces the final
    # artifact at the requested output path (remote render + download).
    fake = FakeTransport([
        httpx.Response(200, json={"filename": "uploaded-scene.png"}),
        httpx.Response(200, json={"task_id": "j-parity"}),
        httpx.Response(200, json={"state": 1, "videos": ["tasks/j-parity/final-1.mp4"]}),
        httpx.Response(200, content=b"external-mp4"),
    ])
    external_out = tmp_path / "external" / "video.mp4"
    MoneyPrinterEngine(url="http://engine:8000", transport=fake).render(
        external_out, **assets
    )
    assert external_out.read_bytes() == b"external-mp4"

    # Parity: same render contract — both engines consumed the same assets
    # (images, durations, aspect ratio, task type) and produced the final output
    # path for the same Job. The submitted request is the second call: the first
    # one uploads the approved scene material.
    spec = fake.requests[1][2]["json"]
    assert [material["original_name"] for material in
            spec["video_materials"]] == [assets["images"][0].name]
    assert spec["video_materials"][0]["url"] == "uploaded-scene.png"
    assert spec["custom_audio_file"] == str(assets["audio"])
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

    assert parse("tasks/t1/final-1.mp4") == "tasks/t1/final-1.mp4"
    assert parse("/tasks/t1/final-1.mp4") == "tasks/t1/final-1.mp4"
    assert (
        parse("https://engine:8000/tasks/t1/final-1.mp4") == "tasks/t1/final-1.mp4"
    )

    for unsafe in ("", "   ", "../etc/passwd", "/etc/passwd", "final-1.mp4"):
        with pytest.raises(BridgeContractError) as exc:
            parse(unsafe)
        assert exc.value.code in {
            "upstream_output_path_unsafe",
            "upstream_result_missing_output",
        }


def test_generic_profile_keeps_legacy_render_contract(tmp_path):
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
        httpx.Response(200, content=b"generic-mp4"),
    ])
    output = ShortGPTEngine(url="http://engine:9000", transport=fake).render(
        tmp_path / "final" / "v.mp4", **assets
    )

    assert output.read_bytes() == b"generic-mp4"


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
    """Material upload wrapper sends files and records SHA256 (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"filename": "uploaded-scene.png"}),
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
    assert uploaded[0]["url"] == "uploaded-scene.png"
    assert uploaded[0]["provider"] == "local"
    assert len(fake.requests) == 1
    assert fake.requests[0][1].endswith("/api/v1/video_materials")
    assert "file" in fake.requests[0][2]["files"]


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


def test_health_check_fails_without_url():
    """Health check fails when URL not configured (Issue #122 P2)."""
    engine = MoneyPrinterEngine(url="")
    health = engine.health_check()

    assert health["available"] is False
    assert health["endpoint_reachable"] is False
    assert health["error"] is not None
    assert "MONEY_PRINTER_URL" in health["error"]


def test_health_check_reports_endpoint_reachable():
    """Health check reports endpoint reachability (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"data": []}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    health = engine.health_check()

    assert health["available"] is True
    assert health["endpoint_reachable"] is True
    assert health["error"] is None


def test_health_check_reports_schema_compatibility():
    """Health check checks schema compatibility (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"data": []}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    health = engine.health_check()

    assert health["schema_compatible"] is True


def test_health_check_includes_pinned_upstream_reference():
    """Health check includes pinned upstream reference (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"data": []}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    health = engine.health_check()

    assert health["pinned_upstream"] == MONEY_PRINTER_CONTRACT.upstream_reference


def test_health_check_reports_snapshot_verification():
    """Health check attempts to verify runtime snapshot (Issue #122 P2)."""
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
        "dependencies": []
    }
    fake = FakeTransport([
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, text=json.dumps(manifest_data)),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    health = engine.health_check()

    assert health["runtime_manifest"] is not None
    assert health["snapshot_verified"] is True


def test_capabilities_includes_health_check():
    """Capabilities endpoint includes health check for MoneyPrinterEngine (Issue #122 P2)."""
    fake = FakeTransport([
        httpx.Response(200, json={"data": []}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    caps = engine.capabilities()

    assert "health" in caps
    assert caps["health"]["available"] is True
    assert caps["ready"] is True


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