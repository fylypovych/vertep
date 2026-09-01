import json

from fastapi.testclient import TestClient

from core.app import app
from core.deployment_plan import create_plan
from core.version import application_version


def test_core_roles_api_exposes_and_queues_composite_plan(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    roles = {
        "core": {"label": "Core", "services": ["core", "proxy"],
                 "capabilities": ["scheduling"], "modules": ["core"]},
        "text": {"label": "Text", "services": ["worker", "ollama"],
                 "capabilities": ["text_generation"], "modules": ["worker"]},
    }
    roles_path = config / "node_roles.json"
    roles_path.write_text(json.dumps(roles), encoding="utf-8")
    base = create_plan(roles, "core", application_version())
    (config / "deployment-plan.json").write_text(json.dumps(base), encoding="utf-8")
    (config / "installation.json").write_text(json.dumps({"completed_at": "2026-01-01T00:00:00Z",
                                                             "node_role": "core"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("NODE_ROLES_FILE", str(roles_path))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("USERS_JSON", raising=False)

    client = TestClient(app)
    status = client.get("/api/system/roles")
    assert status.status_code == 200
    assert status.json()["available_roles"][0]["id"] == "text"
    response = client.post("/api/system/roles", json={"roles": ["text"]})
    assert response.status_code == 200
    assert response.json()["state"] == "QUEUED"
    request = json.loads((config / "deployment-request.json").read_text(encoding="utf-8"))
    expected = create_plan(roles, "core", application_version(), ["text"])
    assert request["additional_roles"] == ["text"]
    assert request["plan_sha256"] == expected["sha256"]


def test_core_roles_api_rejects_unknown_role(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    roles = {"core": {"label": "Core", "services": ["core"],
                      "capabilities": [], "modules": ["core"]}}
    roles_path = config / "node_roles.json"
    roles_path.write_text(json.dumps(roles), encoding="utf-8")
    (config / "installation.json").write_text(json.dumps({"completed_at": "2026-01-01T00:00:00Z",
                                                             "node_role": "core"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("NODE_ROLES_FILE", str(roles_path))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("USERS_JSON", raising=False)
    response = TestClient(app).post("/api/system/roles", json={"roles": ["unknown"]})
    assert response.status_code == 422
