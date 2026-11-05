"""Additional roles run on this node; leftover containers do not degrade it."""

import json

import pytest

from core import role_runtime


@pytest.fixture
def inventory(monkeypatch, tmp_path):
    path = tmp_path / "inventory.json"
    monkeypatch.setenv("RUNTIME_INVENTORY_PATH", str(path))
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setenv("NODE_ADDITIONAL_ROLES", "voice")
    monkeypatch.setattr(role_runtime, "node_roles", lambda: {
        "voice": {"services": ["worker", "tts"]}, "gpu": {"services": ["worker", "comfyui"]}})

    def write(states):
        path.write_text(json.dumps({"services": list(states), "containers": [
            {"service": name, "state": state} for name, state in states.items()]}), encoding="utf-8")
    return write


def test_running_additional_role_is_ready(inventory):
    inventory({"worker": "running", "tts": "running"})
    assert role_runtime.role_runtime_status("voice")["runtime_status"] == "READY"


@pytest.mark.parametrize("states", [{"worker": "running"}, {"worker": "running", "tts": "exited"}])
def test_missing_or_stopped_service_degrades_the_role(inventory, states):
    inventory(states)
    assert role_runtime.role_runtime_status("voice")["runtime_status"] == "DEGRADED"


def test_additional_role_without_inventory_is_unknown(inventory):
    assert role_runtime.role_runtime_status("voice")["runtime_status"] == "UNKNOWN"


def test_unselected_role_is_not_measured_locally(inventory):
    inventory({"worker": "running"})
    assert role_runtime.role_runtime_status("gpu")["evidence"]["source"] == "nodes"
