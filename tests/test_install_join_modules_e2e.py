"""i.0.0.4.96 (#496) AC1/AC3/AC4/AC6: first install -> node join -> module choice.

One disposable contour: a real CORE process (uvicorn) on a fresh configuration
with disposable PostgreSQL/Redis, the real setup API, one-time Registration
Token enrollment with a lost-response retry, Worker heartbeats carrying the
Worker's own module inventory, the administrator's module catalog/lifecycle API
and node revocation. Only the joining node itself is represented by HTTP calls
from this test; no manual ``.env`` editing is part of the user path.
"""

import json
from datetime import datetime, timezone

import httpx
import pytest

from tests.test_node_process_acceptance import (  # noqa: F401  (fixtures)
    config_contour, module_postgres, node_dependencies, redis_url, requires_openssl)

SETUP = {"X-Vertep-Setup-Token": "setup-acceptance-code"}


@pytest.fixture
def module_runtime_env(tmp_path, monkeypatch):
    """Dynamic workflows + module lifecycle for the CORE process (set before it starts)."""
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "c.json").write_text(json.dumps({"name": "C", "version": "1.0.0",
                                                    "tasks": [{"id": "a", "type": "text_generation"}]}))
    for name, value in {"WORKFLOW_DEFINITIONS_ROOT": str(definitions),
                        "VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "module-lifecycle"),
                        "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "module-placement.json"),
                        "WORKFLOW_STATE_ROOT": str(tmp_path / "workflow-state"),
                        "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "module-audit.jsonl"),
                        "VERTEP_MODULE_DRAIN_SECONDS": "1"}.items():
        monkeypatch.setenv(name, value)


def _entry(api, module_id):
    catalog = api.get("/api/nodes/module-catalog")
    assert catalog.status_code == 200, catalog.text
    return next(m for m in catalog.json()["modules"] if m["module_id"] == module_id)


def _node(entry, node_id):
    return next(n for n in entry["nodes"] if n["node_id"] == node_id)


def _ready(entry):
    return [n["node_id"] for n in entry["nodes"] if (n.get("instance") or {}).get("state") == "READY"]


@requires_openssl
@pytest.mark.parametrize("config_contour", ["fresh"], indirect=True)
def test_first_install_join_retry_module_placement_readiness_and_revoke(module_runtime_env, config_contour,
                                                                         tmp_path, monkeypatch):
    c = config_contour
    url = c["url"]
    # -- AC1: the first central machine installs CORE with the chosen modules --------
    status = httpx.get(url + "/api/setup", headers=SETUP, timeout=30).json()
    assert "core" in status["roles"] and "text" in status["roles"]
    chosen = httpx.post(url + "/api/setup/selection", headers=SETUP, timeout=30,
                        json={"node_role": "core", "ai_backend": "skip", "additional_roles": ["text"]})
    assert chosen.status_code == 200, chosen.text
    request = json.loads((c["config"] / "deployment-request.json").read_text())
    assert request["role"] == "core" and request["additional_roles"] == ["text"]  # no second CORE, no LAB
    from tests.test_provider_process_acceptance import PASSWORD
    completed = httpx.post(url + "/api/setup/complete", headers=SETUP, timeout=60, json={
        "node_role": "core", "installation_name": "Перша машина", "username": "admin", "password": PASSWORD,
        "password_confirmation": PASSWORD, "ai_backend": "skip"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["registration_token"]["token"]
    again = httpx.post(url + "/api/setup/complete", headers=SETUP, timeout=30, json={"node_role": "core"})
    assert again.status_code in {401, 403, 409}, again.text  # single use: no second install over the first
    api = c["api"]  # the administrator created above

    # -- AC1/AC6: a new CPU machine joins through one-time enrollment, with retry -------
    token = api.post("/api/nodes/registration-tokens", json={"role": "text"}).json()["token"]
    from core.node_registry import create_node_csr
    csr = create_node_csr("cpu-node", tmp_path / "joining-node" / "pki")
    enroll = {"registration_token": token, "node_id": "cpu-node", "csr": csr, "capabilities": ["text_generation"],
              "hardware": {"gpu_available": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192},
              "version": "test", "enrollment_id": "cpu-node-enrollment"}
    first = httpx.post(url + "/api/nodes/register", json=enroll, timeout=30)
    assert first.status_code == 200, first.text
    retried = httpx.post(url + "/api/nodes/register", json=enroll, timeout=30)  # the first response was "lost"
    assert retried.status_code == 200, retried.text
    nodes = [n for n in api.get("/api/nodes").json() if n["node_id"] == "cpu-node"]
    assert len(nodes) == 1 and nodes[0]["credential_generation"] == 2
    reused = httpx.post(url + "/api/nodes/register", timeout=30,
                        json={**enroll, "node_id": "intruder", "enrollment_id": "other"})
    assert reused.status_code == 401, reused.text  # the token is single use
    jwt = retried.json()["jwt"]

    # The joining Worker reports its own built-in module inventory for its role.
    monkeypatch.setenv("NODE_ROLE", "text")
    from worker.migration import inventory
    worker_inventory = inventory()
    assert [item["name"] for item in worker_inventory] == ["text"]

    def heartbeat(credential):
        return httpx.post(url + "/api/workers/heartbeat", headers={"X-Vertep-Token": credential}, timeout=30, json={
            "node_name": "cpu-node", "role": "text", "status": "FREE", "runtime_instance_id": "cpu-node-1",
            "capabilities": ["text_generation"], "gpu_available": False, "vram_mb": 0, "cpu_cores": 4,
            "ram_mb": 8192, "module_inventory": worker_inventory,
            "self_test": {"status": "PASSED", "role": "text", "checked_at": datetime.now(timezone.utc).isoformat()}})
    assert heartbeat(first.json()["jwt"]).status_code == 401  # superseded by the retry
    accepted = heartbeat(jwt)
    assert accepted.status_code == 200, accepted.text

    # -- AC2/AC4: the administrator chooses module x node with honest compatibility ---
    text = _entry(api, "text")
    assert text["state"] == "NOT_INSTALLED" and _node(text, "cpu-node")["compatible_reasons"] == []
    image = _entry(api, "image")
    assert any("GPU" in reason for reason in _node(image, "cpu-node")["compatible_reasons"])
    for module_id in ("text", "image"):
        installed = api.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": module_id})
        assert installed.status_code == 200, installed.text
    refused = api.put("/api/nodes/module-registry/placement/image", json={"node_ids": ["cpu-node"]})
    assert refused.status_code == 422 and "GPU" in refused.json()["detail"]
    placed = api.put("/api/nodes/module-registry/placement/text", json={"node_ids": ["cpu-node"]})
    assert placed.status_code == 200, placed.text
    assert heartbeat(jwt).status_code == 200
    activated = api.post("/api/nodes/module-registry/lifecycle/activate", json={"module_id": "text"})
    assert activated.status_code == 200 and activated.json()["state"] == "ACTIVE", activated.text
    text = _entry(api, "text")
    assert text["state"] == "ACTIVE" and text["placement"] == ["cpu-node"]
    assert _ready(text) == ["cpu-node"], text["nodes"]
    image = _entry(api, "image")  # the incompatible GPU module never becomes READY, nor blocks text
    assert image["state"] == "INSTALLED" and image["placement"] == [] and _ready(image) == []

    # -- AC6: revocation ends the node's credentials and its module readiness ---------
    revoked = api.post("/api/nodes/cpu-node/revoke")
    assert revoked.status_code == 200, revoked.text
    assert heartbeat(jwt).status_code == 401
    text = _entry(api, "text")
    assert _ready(text) == [], text["nodes"]
    extensions = api.get("/api/modules/ui-extensions")
    assert extensions.status_code == 200 and extensions.json()["extensions"] == []
