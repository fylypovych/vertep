"""Phase 5 tests: VideoEngine pattern (native default + opt-in external engines).

Covers the opt-in/fallback behaviour of the video-engine factory, the native
FFmpeg render path, the remote (MoneyPrinter / ShortGPT) HTTP flow against a
``FakeTransport``, and parity between the native and external engines when
driven from the **same** Job assets.
"""

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


def _job_assets(tmp_path):
    images = [_image(tmp_path / "scene.ppm")]
    return {
        "images": images,
        "clips": None,
        "durations": [0.3],
        "audio": None,
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
    fake = FakeTransport([
        httpx.Response(200, json={"job_id": "j1"}),
        httpx.Response(200, json={"status": "READY"}),
        httpx.Response(200, content=b"mp4-bytes"),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    assets = _job_assets(tmp_path)
    output = engine.render(tmp_path / "final" / "external.mp4", **assets)

    assert output.read_bytes() == b"mp4-bytes"
    methods = [r[0] for r in fake.requests]
    assert methods == ["POST", "GET", "GET"]
    assert fake.requests[0][1].endswith("/render")
    assert fake.requests[1][1].endswith("/jobs/j1")
    assert fake.requests[2][1].endswith("/download/j1")
    spec = fake.requests[0][2]["json"]
    assert spec["task_type"] == "image"
    assert spec["aspect_ratio"] == "9:16"
    assert spec["durations"] == [0.3]
    assert spec["images"] == [str(assets["images"][0])]


def test_remote_engine_sends_bearer_token(tmp_path):
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


def test_remote_engine_api_error_surfaces_http_code(tmp_path):
    fake = FakeTransport([
        httpx.Response(400, json={"error": {"message": "engine down"}}),
    ])
    engine = MoneyPrinterEngine(url="http://engine:8000", transport=fake)
    with pytest.raises(RuntimeError) as exc:
        engine.render(tmp_path / "v.mp4", **(_job_assets(tmp_path)))
    assert "400" in str(exc.value)
    assert "engine down" in str(exc.value)


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
        httpx.Response(200, json={"job_id": "j-parity"}),
        httpx.Response(200, json={"status": "READY"}),
        httpx.Response(200, content=b"external-mp4"),
    ])
    external_out = tmp_path / "external" / "video.mp4"
    MoneyPrinterEngine(url="http://engine:8000", transport=fake).render(
        external_out, **assets
    )
    assert external_out.read_bytes() == b"external-mp4"

    # Parity: same render contract — both engines consumed the same assets
    # (images, durations, aspect ratio, task type) and produced the final output
    # path for the same Job.
    spec = fake.requests[0][2]["json"]
    assert spec["images"] == [str(assets["images"][0])]
    assert spec["durations"] == assets["durations"]
    assert spec["aspect_ratio"] == assets["aspect_ratio"]
    assert spec["task_type"] == assets["task_type"]


# ---------------------------------------------------------------------------
# Registry swappability
# ---------------------------------------------------------------------------


def test_registry_video_engine_is_swappable():
    reg = get_providers()
    reg.replace("video_engine", MoneyPrinterEngine(url="http://engine:8000"))
    assert isinstance(reg.video_engine(), MoneyPrinterEngine)
    assert isinstance(DefaultAssemblyProvider(object()), object)