"""The static pipeline is the default; dynamic workflows need definitions and a choice."""

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.workflow import mode
from core.workflow.runtime import workflow_runtime


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(mode, "config_root", lambda: tmp_path)
    monkeypatch.delenv("WORKFLOW_DEFINITIONS_ROOT", raising=False)
    return tmp_path


def _definitions(monkeypatch, tmp_path):
    root = tmp_path / "dag"
    root.mkdir()
    (root / "one.yaml").write_text("name: One\nversion: 1.0.0\ntasks: []\n", encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_DEFINITIONS_ROOT", str(root))


def test_default_is_static_and_dynamic_is_refused_without_definitions(config):
    assert mode.workflow_mode_state() == {"mode": "static", "default": "static", "dynamic_available": False}
    assert workflow_runtime() is None
    with pytest.raises(ValueError):
        mode.set_workflow_mode("dynamic")


def test_static_choice_turns_a_configured_runtime_off_and_back_on(config, monkeypatch):
    _definitions(monkeypatch, config)
    assert mode.set_workflow_mode("static")["mode"] == "static"
    assert mode.dynamic_definitions_root() is None and workflow_runtime() is None
    assert mode.set_workflow_mode("dynamic")["mode"] == "dynamic"
    assert mode.dynamic_definitions_root() is not None


def test_api_reads_and_refuses_unavailable_or_unknown_mode(config):
    client = TestClient(app)
    assert client.get("/api/settings/workflow-mode").json()["mode"] == "static"
    assert client.post("/api/settings/workflow-mode", json={"mode": "dynamic"}).status_code == 409
    assert client.post("/api/settings/workflow-mode", json={"mode": "other"}).status_code == 409
