"""Issue #178.1/#178.2: the expected GPU runtime inventory must be delivered.

The GPU node may only become ready when it can prove that its runtime matches a
pinned contract.  The contract has two halves that are produced by two
different mechanisms and must never disagree:

* the **expected** inventory — the immutable component pins of the ComfyUI image
  plus the content of the workflows and models the node serves.  It is produced
  by the standard generator from the very Dockerfile that builds the image and
  is written to ``config/comfyui-runtime-inventory.json``;
* the **observed** report — what the image was actually built from.  The
  Dockerfile writes it at build time and the ComfyUI entrypoint publishes it
  onto the shared config volume, where the Worker reads it (``COMFYUI_ROOT``).

These tests close the delivery chain and prove the positive standard path:
Dockerfile → runtime bundle pins → deployment → expected inventory → readiness.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.comfyui_runtime import (INVENTORY_FORMAT, RuntimeInventoryError,
                                      expected_inventory_from_dockerfile,
                                      pinned_document, write_expected_inventory)

REPO = Path(__file__).parents[1]
GENERATOR = REPO / "scripts" / "generate-comfyui-inventory.py"
DOCKERFILE = REPO / "docker" / "comfyui-nvidia" / "Dockerfile"


def _load_script(name: str):
    path = REPO / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dockerfile_pins() -> dict[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    pins = {}
    for argument in ("COMFYUI_COMMIT", "VHS_COMMIT"):
        match = re.search(rf"^ARG {argument}=([0-9a-f]{{40}})$", text, re.MULTILINE)
        assert match, f"{argument} must be pinned by an immutable commit"
        pins[argument] = match.group(1)
    return pins


def _workflow_root(root: Path) -> Path:
    workflows = root / "storage" / "workflows" / "image"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "demo.json").write_text(json.dumps({"1": {"class_type": "CLIPTextEncode"}}),
                                          encoding="utf-8")
    return root / "storage" / "workflows"


def _models_root(root: Path) -> Path:
    models = root / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "model.safetensors").write_bytes(b"weights")
    return models


def _observed_report(root: Path, components: dict) -> Path:
    """The report the ComfyUI image publishes on the shared config volume."""
    report_root = root / "config" / "comfyui-runtime"
    report_root.mkdir(parents=True, exist_ok=True)
    report = report_root / "runtime-inventory.json"
    report.write_text(json.dumps({"format": INVENTORY_FORMAT, "components": components},
                                 ensure_ascii=False), encoding="utf-8")
    return report


def _gpu_environment(monkeypatch, root: Path, inventory: Path) -> None:
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("COMFYUI_RUNTIME_INVENTORY", str(inventory))
    monkeypatch.setenv("COMFYUI_ROOT", str(root / "config" / "comfyui-runtime"))
    monkeypatch.setenv("WORKFLOWS_ROOT", str(root / "storage" / "workflows"))
    monkeypatch.setenv("MODELS_ROOT", str(root / "models"))


class _Adapter:
    def generate_output(self, workflow, topic, task_type="image"):
        return b"P6\n2 2\n255\n" + b"\x22\x36\x30" * 4, "scene-001.ppm", "image"


# ── Issue #178.1: the standard delivery of the expected inventory ───────────


def test_the_dockerfile_pins_are_the_source_of_the_expected_inventory(tmp_path):
    """The recorded commit is derived from the build argument, never written twice."""
    pins = _dockerfile_pins()
    inventory = expected_inventory_from_dockerfile(
        DOCKERFILE.read_text(encoding="utf-8"),
        workflows_root=_workflow_root(tmp_path), models_root=_models_root(tmp_path))
    assert inventory["format"] == INVENTORY_FORMAT
    assert inventory["components"]["comfyui"]["commit"] == pins["COMFYUI_COMMIT"]
    assert inventory["components"]["ComfyUI-VideoHelperSuite"]["commit"] == pins["VHS_COMMIT"]
    # The served content is pinned as well, so a changed workflow is a drift.
    assert "image/demo.json" in inventory["workflows"]
    assert "model.safetensors" in inventory["models"]


def test_the_standard_generator_writes_the_expected_inventory(tmp_path):
    """``scripts/generate-comfyui-inventory.py`` is the standard delivery path."""
    workflows = _workflow_root(tmp_path)
    _models_root(tmp_path)
    output = tmp_path / "config" / "comfyui-runtime-inventory.json"
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--root", str(tmp_path),
         "--dockerfile", str(DOCKERFILE), "--output", str(output)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert output.is_file()
    inventory = json.loads(output.read_text(encoding="utf-8"))
    pins = _dockerfile_pins()
    assert inventory["components"]["comfyui"]["commit"] == pins["COMFYUI_COMMIT"]
    assert inventory["components"]["ComfyUI-VideoHelperSuite"]["commit"] == pins["VHS_COMMIT"]
    assert inventory["workflows"]["image/demo.json"]["sha256"]
    assert inventory["models"]["model.safetensors"]["sha256"]
    # The document is written where the Worker expects it.
    assert (tmp_path / "config" / "comfyui-runtime-inventory.json").is_file()
    assert workflows.is_dir()


def test_the_standard_generator_refuses_a_floating_pin(tmp_path):
    """A pin that is not an immutable commit must not become an inventory."""
    floating = tmp_path / "Dockerfile"
    floating.write_text("ARG COMFYUI_COMMIT=main\nARG VHS_COMMIT=main\n", encoding="utf-8")
    with pytest.raises(RuntimeInventoryError, match="immutable commit"):
        expected_inventory_from_dockerfile(floating.read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--root", str(tmp_path),
         "--dockerfile", str(floating), "--output", str(tmp_path / "config" / "inventory.json")],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 1
    assert "immutable commit" in result.stderr
    assert not (tmp_path / "config" / "inventory.json").exists()


def test_the_runtime_bundle_carries_the_same_pins_as_the_dockerfile():
    """The host has no source checkout, so the bundle must carry the pins."""
    pins = _dockerfile_pins()
    document = pinned_document(DOCKERFILE.read_text(encoding="utf-8"))
    assert document["format"] == INVENTORY_FORMAT
    assert document["components"]["comfyui"]["commit"] == pins["COMFYUI_COMMIT"]
    assert document["components"]["ComfyUI-VideoHelperSuite"]["commit"] == pins["VHS_COMMIT"]
    bundle = (REPO / "scripts" / "build-runtime-bundle.py").read_text(encoding="utf-8")
    assert "config/comfyui-runtime-pins.json" in bundle
    assert "pinned_document(" in bundle


def test_the_deployment_writes_the_expected_inventory_from_the_bundle_pins(tmp_path):
    """The deployment turns the bundle pins into the inventory of the node."""
    apply_deployment = _load_script("apply-deployment.py")
    pins = _dockerfile_pins()
    config = tmp_path / "config"
    config.mkdir(parents=True)
    (config / "comfyui-runtime-pins.json").write_text(
        json.dumps(pinned_document(DOCKERFILE.read_text(encoding="utf-8"))), encoding="utf-8")
    _workflow_root(tmp_path)
    _models_root(tmp_path)

    inventory = apply_deployment.gpu_runtime_inventory(tmp_path)
    assert inventory is not None
    written = config / "comfyui-runtime-inventory.json"
    assert written.is_file()
    document = json.loads(written.read_text(encoding="utf-8"))
    assert document["components"]["comfyui"]["commit"] == pins["COMFYUI_COMMIT"]
    assert document["components"]["ComfyUI-VideoHelperSuite"]["commit"] == pins["VHS_COMMIT"]
    assert "image/demo.json" in document["workflows"]


def test_the_deployment_without_bundle_pins_leaves_the_node_without_an_inventory(tmp_path):
    """A bundle without the pins must not invent an inventory for the node."""
    apply_deployment = _load_script("apply-deployment.py")
    (tmp_path / "config").mkdir(parents=True)
    assert apply_deployment.gpu_runtime_inventory(tmp_path) is None
    assert not (tmp_path / "config" / "comfyui-runtime-inventory.json").exists()


# ── Issue #178.2: the generated inventory decides the readiness ─────────────


def test_the_generated_inventory_makes_the_gpu_node_ready(monkeypatch, tmp_path):
    """The positive standard path: a delivered inventory proves the runtime."""
    from worker.service import role_self_test

    pins = _dockerfile_pins()
    components = {
        "comfyui": {"version": pins["COMFYUI_COMMIT"], "commit": pins["COMFYUI_COMMIT"]},
        "ComfyUI-VideoHelperSuite": {"version": pins["VHS_COMMIT"], "commit": pins["VHS_COMMIT"]},
    }
    inventory = expected_inventory_from_dockerfile(
        DOCKERFILE.read_text(encoding="utf-8"),
        workflows_root=_workflow_root(tmp_path), models_root=_models_root(tmp_path))
    expected = tmp_path / "config" / "comfyui-runtime-inventory.json"
    write_expected_inventory(expected, inventory)
    _observed_report(tmp_path, components)
    _gpu_environment(monkeypatch, tmp_path, expected)

    result = role_self_test("gpu", {"gpu_available": True}, _Adapter())
    assert result["status"] == "PASSED", result


def test_a_drifted_observed_report_keeps_the_gpu_node_out_of_dispatch(monkeypatch, tmp_path):
    """The node must refuse a runtime that was built from another commit."""
    from worker.service import role_self_test

    pins = _dockerfile_pins()
    components = {
        "comfyui": {"version": pins["COMFYUI_COMMIT"], "commit": pins["COMFYUI_COMMIT"]},
        "ComfyUI-VideoHelperSuite": {"version": pins["VHS_COMMIT"], "commit": pins["VHS_COMMIT"]},
    }
    inventory = expected_inventory_from_dockerfile(
        DOCKERFILE.read_text(encoding="utf-8"),
        workflows_root=_workflow_root(tmp_path), models_root=_models_root(tmp_path))
    expected = tmp_path / "config" / "comfyui-runtime-inventory.json"
    write_expected_inventory(expected, inventory)
    drifted = dict(components)
    drifted["ComfyUI-VideoHelperSuite"] = {"version": pins["VHS_COMMIT"], "commit": "c" * 40}
    _observed_report(tmp_path, drifted)
    _gpu_environment(monkeypatch, tmp_path, expected)

    result = role_self_test("gpu", {"gpu_available": True}, _Adapter())
    assert result["status"] == "FAILED"
    assert "ComfyUI-VideoHelperSuite" in result["error"]


def test_a_tampered_workflow_keeps_the_gpu_node_out_of_dispatch(monkeypatch, tmp_path):
    """A workflow that changed after the inventory was generated is a drift."""
    from worker.service import role_self_test

    pins = _dockerfile_pins()
    components = {
        "comfyui": {"version": pins["COMFYUI_COMMIT"], "commit": pins["COMFYUI_COMMIT"]},
        "ComfyUI-VideoHelperSuite": {"version": pins["VHS_COMMIT"], "commit": pins["VHS_COMMIT"]},
    }
    workflows = _workflow_root(tmp_path)
    _models_root(tmp_path)
    inventory = expected_inventory_from_dockerfile(
        DOCKERFILE.read_text(encoding="utf-8"),
        workflows_root=workflows, models_root=tmp_path / "models")
    expected = tmp_path / "config" / "comfyui-runtime-inventory.json"
    write_expected_inventory(expected, inventory)
    _observed_report(tmp_path, components)
    _gpu_environment(monkeypatch, tmp_path, expected)
    (workflows / "image" / "demo.json").write_text(json.dumps({"1": {"class_type": "Other"}}),
                                                   encoding="utf-8")

    result = role_self_test("gpu", {"gpu_available": True}, _Adapter())
    assert result["status"] == "FAILED"
    assert "checksum mismatch" in result["error"]
