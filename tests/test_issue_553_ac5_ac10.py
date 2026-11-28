"""#553 AC5 (GET без мережевих probe) та AC10 (ComfyUI без GPU-ролі)."""

import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("синхронний мережевий виклик у звичайному GET")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(httpx, "post", refuse)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def _admin_client():
    from core.app import app
    return TestClient(app)


def test_ordinary_gets_do_no_network_io(no_network, monkeypatch):
    from core.api import settings as settings_api
    from core.api import nodes as nodes_api
    monkeypatch.setattr(nodes_api, "_require_admin", lambda request: "admin")
    request = object()
    providers = settings_api.provider_backends()
    assert "matrix" in providers
    engine = settings_api.effective_video_engine()
    assert isinstance(engine, dict)
    recommendations = nodes_api.module_update_recommendations(request)
    assert isinstance(recommendations, dict)


def _plan(tmp_path, monkeypatch, **plan):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.delenv("COMFYUI_URL", raising=False)
    from core.api import settings as settings_api
    monkeypatch.setattr(settings_api, "config_root", lambda: tmp_path)
    if plan:
        (tmp_path / "deployment-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return settings_api


def test_cpu_only_node_does_not_probe_comfyui(tmp_path, monkeypatch):
    settings_api = _plan(tmp_path, monkeypatch, role="core", additional_roles=["text", "voice"],
                         services=["core", "worker", "tts"])
    urls = []

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            urls.append(url)
            return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(settings_api.httpx, "Client", Client)
    monkeypatch.setattr(settings_api, "provider_matrix", lambda: {})
    result = settings_api.integrations()
    assert result["comfyui"]["status"] == "NOT_DEPLOYED"
    assert "error" not in result["comfyui"]
    assert all("8188" not in url for url in urls) and any("11434" in url for url in urls)


def test_gpu_node_distinguishes_healthy_and_unreachable_comfyui(tmp_path, monkeypatch):
    settings_api = _plan(tmp_path, monkeypatch, role="core", additional_roles=["gpu"], services=["core", "comfyui"])
    monkeypatch.setattr(settings_api, "provider_matrix", lambda: {})

    class Healthy:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return type("R", (), {"status_code": 200})()

    class Down(Healthy):
        def get(self, url): raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")

    monkeypatch.setattr(settings_api.httpx, "Client", Healthy)
    assert settings_api.integrations()["comfyui"]["status"] == "ONLINE"
    monkeypatch.setattr(settings_api.httpx, "Client", Down)
    assert settings_api.integrations()["comfyui"]["status"] == "OFFLINE"


def test_without_a_plan_the_former_probe_is_kept(tmp_path, monkeypatch):
    settings_api = _plan(tmp_path, monkeypatch)
    assert settings_api._comfyui_applicable() is True


def test_explicit_comfyui_host_is_probed_even_without_gpu_role(tmp_path, monkeypatch):
    settings_api = _plan(tmp_path, monkeypatch, role="core", additional_roles=["text"], services=["core"])
    monkeypatch.setenv("COMFYUI_URL", "http://gpu-box.example:8188")
    assert settings_api._comfyui_applicable() is True
