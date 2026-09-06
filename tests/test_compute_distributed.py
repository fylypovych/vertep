"""Phase 4 tests: optional ComfyUI-Distributed compute backend.

Covers the opt-in/fallback behaviour of the compute factory plus the HTTP flow
of ``ComfyUIDistributedProvider`` against a ``FakeTransport`` (no network).
"""

import httpx
import pytest

from adapters.providers import (
    ComfyUIDistributedProvider,
    DefaultComputeProvider,
    get_providers,
    _make_compute,
)
from publishers.transport import FakeTransport

_WORKFLOW = {
    "1": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "{{CHECKPOINT}}", "topic": "{{TOPIC}}"},
    }
}


def _write_workflow(tmp_path, name="img.json") -> str:
    (tmp_path / name).write_text(
        __import__("json").dumps(_WORKFLOW), encoding="utf-8"
    )
    return str(tmp_path / name)


# ---------------------------------------------------------------------------
# Factory: opt-in + fallback
# ---------------------------------------------------------------------------


def test_compute_default_is_vertep_worker(monkeypatch):
    monkeypatch.delenv("VERTEP_COMPUTE_PROVIDER", raising=False)
    assert isinstance(_make_compute(), DefaultComputeProvider)


def test_compute_falls_back_when_distributed_not_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_COMPUTE_PROVIDER", "comfyui-distributed")
    monkeypatch.setenv("COMFYUI_DISTRIBUTED_URL", "")
    assert isinstance(_make_compute(), DefaultComputeProvider)


def test_compute_enables_distributed_when_configured(monkeypatch):
    monkeypatch.setenv("VERTEP_COMPUTE_PROVIDER", "comfyui-distributed")
    monkeypatch.setenv("COMFYUI_DISTRIBUTED_URL", "http://proxy:8188")
    provider = _make_compute()
    assert isinstance(provider, ComfyUIDistributedProvider)


def test_distributed_isolation_not_configured_without_url(monkeypatch):
    monkeypatch.setenv("COMFYUI_DISTRIBUTED_URL", "")
    provider = ComfyUIDistributedProvider()
    assert provider.configured() is False
    with pytest.raises(RuntimeError):
        provider.generate_output("x.json", "topic", "image")
    assert provider.cancel() is False


# ---------------------------------------------------------------------------
# HTTP flow against FakeTransport
# ---------------------------------------------------------------------------


def test_distributed_image_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    monkeypatch.setenv("COMFYUI_CHECKPOINT", "dist.safetensors")
    fake = FakeTransport([
        httpx.Response(200, json={"prompt_id": "p1"}),
        httpx.Response(200, json={
            "p1": {"outputs": {"9": {"images": [
                {"filename": "scene-001.png", "subfolder": "", "type": "output"},
            ]}}},
        }),
        httpx.Response(200, content=b"\x89PNG-fake"),
    ])
    provider = ComfyUIDistributedProvider(
        url="http://proxy:8188", transport=fake
    )

    data, filename, kind = provider.generate_output(
        _write_workflow(tmp_path), "танок", "image"
    )

    assert kind == "image"
    assert filename == "scene-001.png"
    assert data == b"\x89PNG-fake"
    methods = [r[0] for r in fake.requests]
    assert methods == ["POST", "GET", "GET"]
    assert fake.requests[0][1].endswith("/prompt")
    assert "/history/p1" in fake.requests[1][1]
    assert "/view" in fake.requests[2][1]
    payload = fake.requests[0][2]["json"]["prompt"]
    assert payload["1"]["inputs"]["topic"] == "танок"
    assert payload["1"]["inputs"]["ckpt_name"] == "dist.safetensors"


def test_distributed_video_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    fake = FakeTransport([
        httpx.Response(200, json={"prompt_id": "v1"}),
        httpx.Response(200, json={
            "v1": {"outputs": {"10": {"videos": [
                {"filename": "clip-01.mp4", "subfolder": "", "type": "output"},
            ]}}},
        }),
        httpx.Response(200, content=b"mp4-bytes"),
    ])
    provider = ComfyUIDistributedProvider(
        url="http://proxy:8188", transport=fake
    )

    data, filename, kind = provider.generate_output(
        _write_workflow(tmp_path, "vid.json"), "тема", "video"
    )

    assert kind == "video"
    assert filename == "clip-01.mp4"
    assert data == b"mp4-bytes"


def test_distributed_sends_bearer_token_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    fake = FakeTransport([
        httpx.Response(200, json={"prompt_id": "p1"}),
        httpx.Response(200, json={"p1": {"outputs": {}}}),
    ])
    provider = ComfyUIDistributedProvider(
        url="http://proxy:8188", token="s3cret", transport=fake
    )
    with pytest.raises(RuntimeError):
        provider.generate_output(_write_workflow(tmp_path), "t", "image")
    assert fake.requests[0][2]["headers"]["Authorization"] == "Bearer s3cret"


def test_distributed_api_error_surfaces_http_code(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    fake = FakeTransport([
        httpx.Response(400, json={"error": {"message": "proxy down"}}),
    ])
    provider = ComfyUIDistributedProvider(
        url="http://proxy:8188", transport=fake
    )
    with pytest.raises(RuntimeError) as exc:
        provider.generate_output(_write_workflow(tmp_path), "t", "image")
    assert "400" in str(exc.value)
    assert "proxy down" in str(exc.value)


def test_distributed_cancel_posts_interrupt():
    fake = FakeTransport([httpx.Response(200, json={})])
    provider = ComfyUIDistributedProvider(url="http://proxy:8188", transport=fake)
    assert provider.cancel() is True
    assert fake.requests[0][1].endswith("/interrupt")


def test_registry_compute_is_swappable():
    reg = get_providers()
    reg.replace("compute", ComfyUIDistributedProvider(url="http://p:8188"))
    assert isinstance(reg.compute(), ComfyUIDistributedProvider)
