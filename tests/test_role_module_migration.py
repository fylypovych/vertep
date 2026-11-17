"""i.0.0.4.97 (#497): existing role functions under module management.

Real contour: CORE process, Redis queue, the production Text executor with a
controlled Ollama boundary. Proves the actual claim/route switch (not a direct
module.execute), parity of outputs, rollback to the legacy route, blocking of a
deactivated function, survival of a CORE restart and version compatibility.
"""

import json
from datetime import datetime, timezone

import pytest

from tests.test_text_redis_process import ROOT, TOKEN, claim, contour, create, get_job, result, wait  # noqa: F401
from worker import migration

NODE = "text-process"


@pytest.fixture
def switch_contour(tmp_path, monkeypatch, request):
    definitions = tmp_path / "dag"
    definitions.mkdir()
    (definitions / "configured.json").write_text(json.dumps({"name": "C", "version": "1.0.0",
                                                            "tasks": [{"id": "a", "type": "text_generation"}]}))
    for name, value in {"WORKFLOW_DEFINITIONS_ROOT": str(definitions), "WORKFLOW_STATE_ROOT": str(tmp_path / "dag-state"),
                        "VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lifecycle"),
                        "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "placement.json"),
                        "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "audit.jsonl"), "VERTEP_MODULE_DRAIN_SECONDS": "2",
                        "NODE_ROLE": "text"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("VERTEP_TEXT_MODULE_ENABLED", raising=False)
    yield from contour.__wrapped__(tmp_path, monkeypatch, request)


def beat(api, version=None):
    inventory = migration.inventory()  # the Worker's real report for NODE_ROLE=text
    if version:
        inventory = [{**item, "version": version} for item in inventory]
    body = {"node_name": NODE, "role": "text", "status": "FREE", "vram_mb": 0,
            "capabilities": ["text_generation"], "supported_tasks": ["text"], "module_inventory": inventory,
            "voice_catalog": {"voices": ["uk"], "models": ["process-model"]},
            "self_test": {"status": "PASSED", "role": "text", "checked_at": datetime.now(timezone.utc).isoformat()}}
    response = api.post("/api/workers/heartbeat", json=body)
    assert response.status_code == 200, response.text


def lifecycle(api, action, **body):
    return api.post(f"/api/nodes/module-registry/lifecycle/{action}", json=body, timeout=120)


def last_path():
    return migration.overview()["text"]["last_execution"]["path"]


def script_of(artifacts):
    import base64
    item = next(a for a in artifacts if a["kind"] == "script")
    return json.loads(base64.b64decode(item["data_base64"]))


def test_route_switch_parity_rollback_and_restart(switch_contour):
    api = switch_contour["api"]
    beat(api)
    assert [i["module_id"] for i in api.get("/api/nodes/module-registry").json()["instances"]] == ["text"]
    assert api.get("/api/nodes/module-registry").json()["instances"][0]["reasons"] == ["bundled_not_managed"]

    # 1. Not managed: the legacy role path, unchanged.
    create(api)
    legacy_task = claim(api)
    assert "module_name" not in legacy_task
    response, legacy = result(api, legacy_task)
    assert response.status_code == 200 and last_path() == "role"

    # 2. Installed only: still the legacy route.
    assert lifecycle(api, "install", bundled="text").json()["state"] == "INSTALLED"
    beat(api)
    create(api)
    task = claim(api)
    assert "module_name" not in task
    result(api, task)

    # 3. Placed + ACTIVE: the same queue/lease, executed through the module path.
    assert api.put("/api/nodes/module-registry/placement/text", json={"node_ids": [NODE]}).status_code == 200
    assert lifecycle(api, "activate", module_id="text").json()["state"] == "ACTIVE"
    beat(api)
    job = create(api)
    routed = claim(api)
    assert routed["module_name"] == "text" and routed["module_routed"] is True
    assert routed["module_version"] == migration.inventory()[0]["version"]
    response, module_output = result(api, routed)
    assert response.status_code == 200 and last_path() == "module"
    wait(lambda: get_job(api, job)["status"] == "SCRIPT_PENDING_APPROVAL")
    # Parity on the same executor and boundary: identical script content.
    assert script_of(module_output) == script_of(legacy)

    # 4. CORE restart: lifecycle state persists; routing resumes after the next report.
    switch_contour["restart"]("core")
    beat(api)
    create(api)
    after_restart = claim(api)
    assert after_restart["module_name"] == "text"
    result(api, after_restart)

    # 5. Deactivated: no node may claim; the task waits instead of falling back.
    assert lifecycle(api, "deactivate", module_id="text").json()["state"] == "INACTIVE"
    beat(api)
    waiting = create(api)
    assert claim(api) is None
    assert get_job(api, waiting)["status"] == "SCRIPT_QUEUED"

    # 6. Version mismatch between CORE record and Worker report: no false READY.
    beat(api, version="9.9.9")
    activation = lifecycle(api, "activate", module_id="text")
    assert activation.status_code == 409 and "self-test" in activation.text
    assert claim(api) is None

    # 7. Rollback of the switch: uninstall returns the function to the legacy route.
    beat(api)
    assert lifecycle(api, "uninstall", module_id="text", confirm=True).status_code == 200
    beat(api)
    resumed = claim(api)
    assert resumed is not None and "module_name" not in resumed
    response, _ = result(api, resumed)
    assert response.status_code == 200 and last_path() == "role"
    wait(lambda: get_job(api, waiting)["status"] == "SCRIPT_PENDING_APPROVAL")


def test_routed_task_cancel_fences_a_stale_module_result(switch_contour):
    from tests.test_text_redis_process import consistent
    api = switch_contour["api"]
    beat(api)
    assert lifecycle(api, "install", bundled="text").status_code == 200
    assert api.put("/api/nodes/module-registry/placement/text", json={"node_ids": [NODE]}).status_code == 200
    beat(api)
    assert lifecycle(api, "activate", module_id="text").json()["state"] == "ACTIVE"
    beat(api)
    job_id = create(api)
    issued = claim(api)
    assert issued["module_routed"] is True
    assert api.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    response, _ = result(api, issued)  # the module path finishes after cancellation
    assert response.status_code == 409  # stale result fenced exactly as on the role path
    beat(api)
    assert claim(api) is None
    assert get_job(api, job_id)["status"] == "CANCELLED"
    assert consistent(switch_contour)["total"]["ready"] == consistent(switch_contour)["total"]["inflight"] == 0
