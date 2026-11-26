"""i.0.0.5.26 (#526): Workflow validation placeholders, automatic reference usage & cleanup."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from adapters.comfyui import (
    REFERENCE_PLACEHOLDER,
    SUPPORTED_PLACEHOLDERS as ADAPTER_PLACEHOLDERS,
    _substitute_placeholders,
    apply_references,
    placeholder_values,
)
from core import state
from core.app import app
from core.character_references import save_reference
from core.workflows import (
    SUPPORTED_PLACEHOLDERS as VALIDATOR_PLACEHOLDERS,
    WorkflowRegistry,
    validate_workflow,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"ref-sample-bytes"


def test_validator_and_adapter_share_single_placeholder_source():
    """AC2: Validator and ComfyUI adapter placeholders come from one source without divergence."""
    assert VALIDATOR_PLACEHOLDERS is ADAPTER_PLACEHOLDERS
    runtime_keys = set(placeholder_values("test topic").keys()) | {REFERENCE_PLACEHOLDER}
    assert VALIDATOR_PLACEHOLDERS == runtime_keys


def test_ip_adapter_workflow_valid_and_unknown_placeholder_rejected():
    """AC1: image/ip-adapter.json validates as valid; unsupported placeholder fails."""
    ip_adapter = json.loads(Path("workflows/image/ip-adapter.json").read_text(encoding="utf-8"))
    report = validate_workflow(ip_adapter)
    assert report["valid"] is True, report["errors"]
    assert {"CHECKPOINT", "IPADAPTER_MODEL", "CLIP_VISION_MODEL", "REFERENCE_IMAGE", "TOPIC"} <= set(
        report["schema"]["has_placeholders"]
    )

    broken = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{UNSUPPORTED_CUSTOM_VAR}}"}},
    }
    bad_report = validate_workflow(broken)
    assert bad_report["valid"] is False
    assert any("UNSUPPORTED_CUSTOM_VAR" in err for err in bad_report["errors"])


def test_all_bundled_workflows_are_valid_and_test_leftovers_removed():
    """AC1 & AC4: Bundled workflows are valid; test_persist.json & test.json.meta.json removed."""
    assert not Path("workflows/image/test_persist.json").exists()
    assert not Path("workflows/image/test.json.meta.json").exists()

    registry = WorkflowRegistry("workflows")
    items = registry.list()
    by_path = {item["path"]: item for item in items}
    assert "workflows/image/ip-adapter.json" in by_path
    assert "workflows/image/test_persist.json" not in by_path
    for item in items:
        assert item["valid"] is True, f"{item['path']}: {item['errors']}"


def test_ip_adapter_automatic_usage_and_delete_guard(tmp_path, monkeypatch):
    """AC3: ip-adapter.json shows automatic usage for characters with references and blocks unforced delete."""
    wf_root = tmp_path / "workflows"
    shutil.copytree("workflows", wf_root)
    char_root = tmp_path / "characters"
    char_root.mkdir()

    # Character with default workflow + reference image -> uses ip-adapter.json automatically
    c_ref = char_root / "hero_ref"
    c_ref.mkdir()
    (c_ref / "character.json").write_text(json.dumps({"id": "hero_ref", "workflow": "workflows/image/demo.json"}))
    # Character with explicit custom workflow + reference image -> must NOT show under ip-adapter.json
    c_custom = char_root / "hero_custom"
    c_custom.mkdir()
    (c_custom / "generation.json").write_text(json.dumps({"workflow": "workflows/image/custom.json"}))
    # Character with default workflow and NO references -> must NOT show under ip-adapter.json
    c_plain = char_root / "hero_plain"
    c_plain.mkdir()
    (c_plain / "character.json").write_text(json.dumps({"id": "hero_plain", "workflow": "workflows/image/demo.json"}))

    monkeypatch.setenv("CHARACTERS_ROOT", str(char_root))
    save_reference("hero_ref", _PNG)
    save_reference("hero_custom", _PNG)

    registry = WorkflowRegistry(wf_root)
    monkeypatch.setattr(state, "workflow_registry", registry)
    client = TestClient(app)

    # Usage endpoint reports automatic role + hero_ref (not hero_custom or hero_plain)
    usage_resp = client.get("/api/workflows/image/ip-adapter.json/usage")
    assert usage_resp.status_code == 200
    usage = usage_resp.json()
    assert usage["total_automatic"] == 1
    assert usage["automatic"][0]["scope"] == "character_references"
    char_ids = [c["character_id"] for c in usage["characters"]]
    assert char_ids == ["hero_ref"]
    assert usage["characters"][0]["automatic"] is True

    # Delete without force=true is blocked with 409 WORKFLOW_IN_USE (same as character workflow)
    del_refused = client.delete("/api/workflows/image/ip-adapter.json")
    assert del_refused.status_code == 409
    detail = del_refused.json()["detail"]
    assert detail["error_code"] == "WORKFLOW_IN_USE"
    assert detail["usage"]["automatic"]
    assert [c["character_id"] for c in detail["usage"]["characters"]] == ["hero_ref"]
    assert (wf_root / "image" / "ip-adapter.json").is_file()

    # Even with zero characters having references, automatic CORE usage still blocks unforced delete
    shutil.rmtree(c_ref / "references")
    del_refused_auto = client.delete("/api/workflows/image/ip-adapter.json")
    assert del_refused_auto.status_code == 409
    assert del_refused_auto.json()["detail"]["usage"]["automatic"]

    # Explicit force=true succeeds (same contract as character workflow)
    del_forced = client.delete("/api/workflows/image/ip-adapter.json", params={"force": "true"})
    assert del_forced.status_code == 200
    assert not (wf_root / "image" / "ip-adapter.json").exists()


def test_bundled_production_workflows_graph_integrity_and_substitution():
    """Verify every bundled image/video/character workflow has valid links, outputs & substitution."""
    expected_paths = {
        "workflows/image/demo.json",
        "workflows/image/ip-adapter.json",
        "workflows/image/sdxl-16x9.json",
        "workflows/image/sdxl-9x16.json",
        "workflows/image/ip-adapter-9x16.json",
        "workflows/image/hires-16x9.json",
        "workflows/video/demo.json",
        "workflows/video/sdxl-16x9.json",
        "workflows/video/sdxl-9x16.json",
        "workflows/character/portrait-sheet.json",
        "workflows/character/ip-adapter-consistency.json",
    }
    registry = WorkflowRegistry("workflows")
    listed = {item["path"]: item for item in registry.list()}
    assert expected_paths <= set(listed.keys())

    for rel_path in sorted(expected_paths):
        doc = json.loads(Path(rel_path).read_text(encoding="utf-8"))
        report = validate_workflow(doc)
        assert report["valid"] is True and not report["warnings"], f"{rel_path}: {report}"

        # Every node connection ["<node_id>", <slot>] must point to an existing node
        node_ids = set(doc.keys())
        class_types = set()
        for node_id, node in doc.items():
            class_types.add(node["class_type"])
            for inp_val in (node.get("inputs") or {}).values():
                if isinstance(inp_val, list) and len(inp_val) == 2 and isinstance(inp_val[0], str):
                    assert inp_val[0] in node_ids, f"{rel_path}: node {node_id} links to missing {inp_val[0]}"

        # Must contain an output node matching its workflow kind
        if rel_path.startswith("workflows/video/"):
            assert "VHS_VideoCombine" in class_types
        else:
            assert "SaveImage" in class_types

        # Substituting runtime values + references leaves zero unresolved {{...}} placeholders
        rendered = _substitute_placeholders(doc, placeholder_values("Український вертеп біля печі"))
        if "{{REFERENCE_IMAGE}}" in json.dumps(doc):
            rendered = apply_references(rendered, ["ref1.png", "ref2.png"])
        assert "{{" not in json.dumps(rendered), f"Unresolved placeholder in {rel_path}"
