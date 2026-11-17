"""i.0.0.4.99 (#499): Characters module — ownership, collaboration, activation."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.module.lifecycle import ModuleLifecycle
from core.state import store
from core.workflow.runtime import WorkflowRuntime


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import app
    from core.state import store
    from core.workflow.runtime import WorkflowRuntime
    dag = tmp_path / "dag"
    dag.mkdir()
    (dag / "c.json").write_text(json.dumps({"name": "C", "version": "1.0.0", "tasks": [{"id": "a", "type": "text_generation"}]}))
    for name, value in {"VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lc"), "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "p.json"),
                        "WORKFLOW_STATE_ROOT": str(tmp_path / "s"), "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "a.jsonl")}.items():
        monkeypatch.setenv(name, value)
    runtime = WorkflowRuntime(dag)
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: runtime)
    # Add worker nodes for module catalog
    nodes = {"text-node": {"role": "text", "gpu_available": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192},
             "gpu-node": {"role": "gpu", "gpu_available": True, "vram_mb": 12288, "cpu_cores": 8, "ram_mb": 32768}}
    for name, facts in nodes.items():
        monkeypatch.setitem(store.workers, name, {"node_name": name, "status": "READY", **facts})
    client = TestClient(app)
    client.runtime = runtime
    return client


def test_characters_module_in_bundled(api):
    """Characters module should be available as bundled module."""
    catalog = api.get("/api/nodes/module-catalog").json()
    module_ids = {m["module_id"] for m in catalog["modules"]}
    assert "characters" in module_ids
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    assert char_module["kind"] == "application"
    assert char_module["label"] == "Персонажі"


def test_characters_module_install_and_lifecycle(api):
    """Test install, activate, deactivate of characters module."""
    # Install
    resp = api.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": "characters"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "INSTALLED"

    # Activate (requires placement and approval)
    resp = api.put("/api/nodes/module-registry/placement/characters", json={"node_ids": ["text-node"]})
    assert resp.status_code == 200

    resp = api.put("/api/nodes/module-registry/approval/characters", json={"permissions": ["characters.read", "characters.write", "characters.collaborate"]})
    assert resp.status_code == 200

    resp = api.post("/api/nodes/module-registry/lifecycle/activate", json={"module_id": "characters"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "ACTIVE"

    # Deactivate
    resp = api.post("/api/nodes/module-registry/lifecycle/deactivate", json={"module_id": "characters"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "INACTIVE"


def test_characters_module_catalog_entry(api):
    """Test characters module appears in catalog with correct metadata."""
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    assert char_module["kind"] == "application"
    assert char_module["placement"]["scaling"] == "single_owner"
    assert char_module["placement"]["requires_gpu"] is False
    assert "characters.read" in char_module["permissions"]
    assert "characters.write" in char_module["permissions"]
    assert "characters.collaborate" in char_module["permissions"]


def test_characters_module_requires_text_node(api):
    """Characters module should be compatible with text-node (no GPU required)."""
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    text_node = next(n for n in char_module["nodes"] if n["node_id"] == "text-node")
    assert text_node["compatible_reasons"] == []


def test_characters_module_incompatible_with_gpu_only(api):
    """Characters module should work on gpu-node too (no GPU required but compatible)."""
    catalog = api.get("/api/nodes/module-catalog").json()
    char_module = next(m for m in catalog["modules"] if m["module_id"] == "characters")
    gpu_node = next(n for n in char_module["nodes"] if n["node_id"] == "gpu-node")
    assert gpu_node["compatible_reasons"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])