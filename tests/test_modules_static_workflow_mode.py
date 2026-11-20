"""Modules work in the default static workflow mode (0.0.4.17 regression).

The module registry and lifecycle lived only inside the dynamic WorkflowRuntime, so a
standard installation (no WORKFLOW_DEFINITIONS_ROOT) answered the «Модулі» page with
409 «Workflow templates are not configured». Nothing is patched here: the real app,
the static mode and a Worker heartbeat with its module inventory.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

TOKEN = "static-mode-node-token"


@pytest.fixture
def static_core(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_DEFINITIONS_ROOT", raising=False)
    for name, value in {"VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lifecycle"),
                        "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "placement.json"),
                        "WORKFLOW_STATE_ROOT": str(tmp_path / "workflow-state"),
                        "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "audit.jsonl"),
                        "NODE_API_TOKEN": TOKEN}.items():
        monkeypatch.setenv(name, value)
    from core.app import app
    from core.workflow.runtime import workflow_runtime
    assert workflow_runtime() is None  # the default static pipeline
    return TestClient(app)


def test_the_module_catalog_registry_and_node_reports_work_without_dynamic_workflows(static_core, monkeypatch):
    monkeypatch.setenv("NODE_ROLE", "text")
    from worker.migration import inventory
    beat = static_core.post("/api/workers/heartbeat", headers={"X-Vertep-Token": TOKEN}, json={
        "node_name": "text-node", "role": "text", "status": "FREE", "runtime_instance_id": "text-node-1",
        "capabilities": ["text_generation"], "gpu_available": False, "vram_mb": 0, "cpu_cores": 4,
        "ram_mb": 8192, "module_inventory": inventory(),
        "self_test": {"status": "PASSED", "role": "text", "checked_at": datetime.now(timezone.utc).isoformat()}})
    assert beat.status_code == 200, beat.text
    assert beat.json()["workflow_execution_enabled"] is False  # still the static pipeline

    catalog = static_core.get("/api/nodes/module-catalog")
    assert catalog.status_code == 200, catalog.text
    modules = {item["module_id"]: item for item in catalog.json()["modules"]}
    assert {"characters", "brands", "storyboard", "publishing", "monitoring"} <= set(modules)
    text_nodes = {node["node_id"]: node for node in modules["text"]["nodes"]}
    assert text_nodes["text-node"]["compatible_reasons"] == []

    registry = static_core.get("/api/nodes/module-registry")
    assert registry.status_code == 200, registry.text
    assert any(item.get("node_id") == "text-node" for item in registry.json().get("instances", []))
    assert static_core.get("/api/modules/ui-extensions").status_code == 200
