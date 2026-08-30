import asyncio
import importlib
from pathlib import Path

import pytest


core_app = importlib.import_module("core.app")


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_ai_backend_validation_checks_model_and_credentials(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return Response({"data": [{"id": "gpt-production"}]})

    monkeypatch.setattr(core_app.httpx, "AsyncClient", Client)
    asyncio.run(core_app._validate_ai_backend(
        "openai", "https://ai.example/v1", "gpt-production", "secret-key"))
    assert calls[0][1] == "https://ai.example/v1/models"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer secret-key"
    with pytest.raises(ValueError, match="API key"):
        asyncio.run(core_app._validate_ai_backend(
            "external", "https://ai.example/v1", "gpt-production", ""))


def test_ollama_validation_can_install_selected_model(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return Response({"models": []})

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return Response({"status": "success"})

    monkeypatch.setattr(core_app.httpx, "AsyncClient", Client)
    asyncio.run(core_app._validate_ai_backend(
        "ollama", "http://ollama:11434", "llama3.2", ""))
    assert calls == [("http://ollama:11434/api/pull", {"name": "llama3.2", "stream": False})]


def test_zero_shell_routes_and_ui_are_present():
    paths = {route.path for route in core_app.app.routes}
    assert {"/api/system/backups", "/api/system/backups/{snapshot_id}/restore",
            "/api/system/models", "/api/system/models/pull",
            "/api/system/certificates", "/api/system/certificates/renew",
            "/api/system/installation-manifest"} <= paths
    ui = (Path(__file__).parents[1] / "web/index.html").read_text(encoding="utf-8")
    assert "Zero-Shell lifecycle" in ui
    assert "restoreBackup" in ui and "deleteModel" in ui and "renewCertificate" in ui


def test_gpu_bootstrap_contains_production_runtimes():
    root = Path(__file__).parents[1]
    bootstrap = (root / "bootstrap.sh").read_text(encoding="utf-8")
    assert "nvidia-container-toolkit" in bootstrap
    assert "nvidia-ctk runtime configure" in bootstrap
    assert "rocm-hip-runtime" in bootstrap and "rocminfo" in bootstrap
    assert (root / "deploy/docker-compose.amd.yml").is_file()
    assert (root / "deploy/docker-compose.nvidia.yml").is_file()


def test_runtime_configuration_has_no_mutable_source_overlays():
    compose = (Path(__file__).parents[1] / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    for mount in ("./runtime/proxy.conf", "./monitoring/prometheus.yml",
                  "./monitoring/loki.yml", "./monitoring/promtail.yml",
                  "./monitoring/grafana/provisioning"):
        assert mount not in compose
