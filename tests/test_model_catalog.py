"""#454: Ollama download catalog, separate from installed models, and pull diagnostics."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import model_catalog
from core.app import app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def _verify_script():
    spec = importlib.util.spec_from_file_location("verify_ollama_catalog", ROOT / "scripts/verify-ollama-catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_api_lists_tagged_library_models_not_installed_ones(monkeypatch):
    body = client.get("/api/system/models/catalog").json()
    assert body["source"].startswith("https://registry.ollama.ai/")
    assert body["verified_at"]
    names = [item["name"] for item in body["models"]]
    assert "llama3.2:3b" in names and len(names) == len(set(names))
    for item in body["models"]:
        # Every entry is a concrete tag the pull API accepts unchanged.
        assert ":" in item["name"] and model_catalog.MODEL_NAME.fullmatch(item["name"])
        assert item["download_bytes"] > 0 and item["family"]


@pytest.mark.parametrize("content", ["not json", json.dumps({"schema": 2, "models": []}),
                                     json.dumps({"schema": 1, "models": [{"name": "bad name", "download_bytes": 1}]}),
                                     json.dumps({"schema": 1, "models": [{"name": "untagged", "download_bytes": 1}]})])
def test_unavailable_or_invalid_catalog_is_reported_not_guessed(tmp_path, monkeypatch, content):
    path = tmp_path / "catalog.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(model_catalog, "CATALOG_PATH", path)
    response = client.get("/api/system/models/catalog")
    assert response.status_code == 503
    assert response.json()["detail"] == "Model catalog is unavailable"
    assert response.json()["code"] == "model_catalog_unavailable"


@pytest.mark.parametrize("name", [" llama3.2:3b", "llama3.2:3b ", "ollama pull llama3.2", ""])
def test_pull_rejects_untrimmed_or_command_names(name):
    response = client.post("/api/system/models/pull", json={"name": name})
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid model name"


def test_verify_script_flags_missing_tags_and_changed_sizes():
    module = _verify_script()
    catalog = {"models": [{"name": "a:1b", "download_bytes": 10}, {"name": "b:1b", "download_bytes": 20},
                          {"name": "c:1b", "download_bytes": 30}]}
    sizes = {"a:1b": 10, "b:1b": None, "c:1b": 31}
    problems = module.verify(catalog, fetch=sizes.get)
    assert problems == ["b:1b: tag is not in the Ollama registry", "c:1b: download size changed 30 -> 31"]
    assert module.verify({"models": [catalog["models"][0]]}, fetch=sizes.get) == []
