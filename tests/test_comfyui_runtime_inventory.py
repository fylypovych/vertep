"""Issue #83: the GPU runtime must be pinned by a verifiable inventory.

The inventory records the components the image was built from plus the
workflow/model content the node executes.  Every negative path (missing
inventory, malformed contract, version/commit drift, missing or tampered file)
must fail closed instead of silently running whatever is on disk.
"""

import json

import pytest

from adapters import comfyui_runtime
from adapters.comfyui import ComfyUIAdapter
from adapters.comfyui_runtime import (INVENTORY_FORMAT, RuntimeInventoryError,
                                      installed_components, load_inventory,
                                      verify_components, verify_file_set, verify_runtime)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def _inventory(tmp_path, **overrides):
    payload = {
        "format": INVENTORY_FORMAT,
        "components": {"comfyui": {"version": "v0.34.0", "commit": COMMIT_A}},
        "workflows": {},
        "models": {},
    }
    payload.update(overrides)
    path = tmp_path / "runtime-inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _digest(path):
    return comfyui_runtime.file_sha256(path)


def test_missing_inventory_file_fails_closed(tmp_path):
    with pytest.raises(RuntimeInventoryError, match="inventory is missing"):
        verify_runtime(path=tmp_path / "absent.json")


def test_unconfigured_inventory_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("COMFYUI_RUNTIME_INVENTORY", raising=False)
    assert comfyui_runtime.inventory_path() is None
    with pytest.raises(RuntimeInventoryError, match="not configured"):
        verify_runtime()


def test_unreadable_inventory_fails_closed(tmp_path):
    broken = tmp_path / "runtime-inventory.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeInventoryError, match="unreadable"):
        load_inventory(broken)


def test_wrong_inventory_format_is_rejected(tmp_path):
    path = _inventory(tmp_path, format="something/v9")
    with pytest.raises(RuntimeInventoryError, match="must use format"):
        load_inventory(path)


def test_inventory_without_components_is_rejected(tmp_path):
    path = _inventory(tmp_path, components={})
    with pytest.raises(RuntimeInventoryError, match="declares no components"):
        load_inventory(path)


def test_matching_components_verify(tmp_path):
    inventory = load_inventory(_inventory(tmp_path))
    assert verify_components(inventory, {"comfyui": {"version": "v0.34.0", "commit": COMMIT_A}}) == []


def test_version_drift_is_reported(tmp_path):
    inventory = load_inventory(_inventory(tmp_path))
    problems = verify_components(inventory, {"comfyui": {"version": "v0.3.3", "commit": COMMIT_A}})
    assert problems and "pinned v0.34.0" in problems[0]


def test_commit_drift_is_reported(tmp_path):
    inventory = load_inventory(_inventory(tmp_path))
    problems = verify_components(inventory, {"comfyui": {"version": "v0.34.0", "commit": COMMIT_B}})
    assert problems and "pinned commit" in problems[0]


def test_absent_component_is_reported(tmp_path):
    inventory = load_inventory(_inventory(tmp_path))
    problems = verify_components(inventory, {})
    assert problems and "installed unknown" in problems[0]


def test_malformed_pinned_commit_is_rejected(tmp_path):
    path = _inventory(tmp_path, components={"comfyui": {"version": "v0.34.0", "commit": "abc"}})
    with pytest.raises(RuntimeInventoryError, match="malformed pinned commit"):
        verify_components(load_inventory(path), {})


def test_workflow_checksum_mismatch_fails_closed(tmp_path):
    workflow = tmp_path / "image" / "demo.json"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("{}", encoding="utf-8")
    good = _digest(workflow)
    inventory = load_inventory(_inventory(tmp_path, workflows={"image/demo.json": {"sha256": good}}))
    assert verify_file_set(inventory["workflows"], tmp_path, "workflow") == []
    workflow.write_text('{"tampered": true}', encoding="utf-8")
    problems = verify_file_set(inventory["workflows"], tmp_path, "workflow")
    assert problems and "checksum mismatch" in problems[0]


def test_missing_workflow_fails_closed(tmp_path):
    inventory = load_inventory(_inventory(
        tmp_path, workflows={"image/gone.json": {"sha256": "c" * 64}}))
    problems = verify_file_set(inventory["workflows"], tmp_path, "workflow")
    assert problems and "missing" in problems[0]


def test_entry_without_valid_sha256_is_rejected(tmp_path):
    inventory = load_inventory(_inventory(tmp_path, models={"model.safetensors": {"sha256": "nope"}}))
    with pytest.raises(RuntimeInventoryError, match="no valid sha256"):
        verify_file_set(inventory["models"], tmp_path, "model")


def test_entry_escaping_its_root_is_rejected(tmp_path):
    inventory = load_inventory(_inventory(tmp_path, workflows={"../escape.json": {"sha256": "d" * 64}}))
    with pytest.raises(RuntimeInventoryError, match="escapes its root"):
        verify_file_set(inventory["workflows"], tmp_path, "workflow")


def test_verify_runtime_reports_every_problem_together(tmp_path):
    inventory = load_inventory(_inventory(
        tmp_path,
        workflows={"image/demo.json": {"sha256": "e" * 64}},
        models={"model.safetensors": {"sha256": "f" * 64}}))
    with pytest.raises(RuntimeInventoryError) as error:
        verify_runtime({"comfyui": {"version": "v0.1.0", "commit": COMMIT_B}},
                       workflows_root=tmp_path, models_root=tmp_path, path=tmp_path / "runtime-inventory.json")
    message = str(error.value)
    assert "workflow image/demo.json: missing" in message
    assert "model model.safetensors: missing" in message
    assert "pinned v0.34.0" in message
    assert inventory["format"] == INVENTORY_FORMAT


def test_verify_runtime_passes_for_a_consistent_installation(tmp_path):
    workflow = tmp_path / "image" / "demo.json"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("{}", encoding="utf-8")
    _inventory(tmp_path, workflows={"image/demo.json": {"sha256": _digest(workflow)}})
    observed = verify_runtime({"comfyui": {"version": "v0.34.0", "commit": COMMIT_A}},
                              workflows_root=tmp_path, models_root=tmp_path,
                              path=tmp_path / "runtime-inventory.json")
    assert observed["workflows"] == ["image/demo.json"]


def test_installed_components_reads_the_image_report(tmp_path):
    root = tmp_path / "comfyui"
    root.mkdir()
    (root / "runtime-inventory.json").write_text(json.dumps(
        {"components": {"comfyui": {"version": "v0.34.0", "commit": COMMIT_A}}}), encoding="utf-8")
    assert installed_components(root)["comfyui"]["commit"] == COMMIT_A
    assert installed_components(tmp_path / "absent") == {}


def test_worker_self_test_blocks_a_drifted_gpu_runtime(monkeypatch, tmp_path):
    """A GPU node whose runtime drifted from its pin must fail its self-test."""
    from worker.service import role_self_test

    inventory = _inventory(tmp_path, components={
        "comfyui": {"version": "v0.34.0", "commit": COMMIT_A},
        "ComfyUI-VideoHelperSuite": {"version": "1.0.4", "commit": COMMIT_B},
    })
    monkeypatch.setenv("COMFYUI_RUNTIME_INVENTORY", str(inventory))
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    monkeypatch.setenv("MODELS_ROOT", str(tmp_path))
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setattr("adapters.comfyui_runtime.installed_components", lambda: {
        "comfyui": {"version": "v0.34.0", "commit": COMMIT_A},
        "ComfyUI-VideoHelperSuite": {"version": "1.0.4", "commit": "c" * 40},
    })
    result = role_self_test("gpu", {"gpu_available": True})
    assert result["status"] == "FAILED"
    assert "ComfyUI-VideoHelperSuite" in result["error"]


def test_worker_self_test_passes_for_a_matching_gpu_runtime(monkeypatch, tmp_path):
    from worker.service import role_self_test

    inventory = _inventory(tmp_path)
    monkeypatch.setenv("COMFYUI_RUNTIME_INVENTORY", str(inventory))
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
    monkeypatch.setenv("MODELS_ROOT", str(tmp_path))
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setattr("adapters.comfyui_runtime.installed_components", lambda: {
        "comfyui": {"version": "v0.34.0", "commit": COMMIT_A}})
    seen = {}

    class _Adapter:
        def generate_output(self, workflow, topic, task_type="image"):
            seen["workflow"] = workflow
            return b"P6\n2 2\n255\n" + b"\x22\x36\x30" * 4, "scene-001.ppm", "image"

    result = role_self_test("gpu", {"gpu_available": True}, _Adapter())
    assert result["status"] == "PASSED"
    assert seen["workflow"] == "workflows/image/demo.json"


def test_workflow_path_has_no_non_persistent_fallback(tmp_path, monkeypatch):
    """A missing workflow must fail instead of resolving against a relative path."""
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path / "persistent"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "image.json").write_text("{}", encoding="utf-8")
    adapter = ComfyUIAdapter()
    with pytest.raises(FileNotFoundError):
        adapter.generate_output("workflows/image.json", "topic")


def test_compose_pins_a_persistent_workflows_root():
    from pathlib import Path

    compose = (Path(__file__).parents[1] / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    worker = compose.split("  worker:", 1)[1].split("  comfyui:", 1)[0]
    # The GPU worker must resolve workflows through the mounted, persistent volume.
    assert "WORKFLOWS_ROOT: /data/storage/workflows" in worker
    assert '"./storage:/data/storage"' in worker
