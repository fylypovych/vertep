"""audit-1511 В1: Worker-only routes reach their node-token check when auth is on.

With an admin password configured, the session middleware answered
"Authentication required" before the route could verify the worker token, so
the routes the Worker calls were unusable. They are now exact machine routes;
everything else stays behind the session.
"""

import json

import pytest
from fastapi.testclient import TestClient

from core.app import app

TOKEN = "worker-routes-token"
WORKER = {"x-vertep-token": TOKEN, "x-vertep-node": "w1", "x-vertep-node-name": "w1"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    (tmp_path / "installation.json").write_text(json.dumps({"completed_at": "2026-01-01"}))
    (tmp_path / "users.json").write_text("{}")
    monkeypatch.setenv("ADMIN_PASSWORD", "routes-admin-password")
    monkeypatch.setenv("NODE_API_TOKEN", TOKEN)
    monkeypatch.delenv("NODE_MTLS_REQUIRED", raising=False)
    return TestClient(app)


CALLS = [
    ("GET", "/api/tasks/publisher-readiness", None),
    ("POST", "/api/workers/model-progress", {"node_name": "w1", "operation_id": "0" * 32}),
    ("POST", "/api/watchdog/report", {}),
    ("POST", "/api/nodes/w1/self-test", {"status": "passed", "capabilities": []}),
]


@pytest.mark.parametrize("method,path,body", CALLS)
def test_worker_token_reaches_the_route(client, method, path, body):
    response = client.request(method, path, json=body, headers=WORKER)
    assert response.text != "Authentication required"
    assert response.status_code != 401, response.text


@pytest.mark.parametrize("method,path,body", CALLS)
def test_wrong_token_is_refused_by_the_route_itself(client, method, path, body):
    response = client.request(method, path, json=body, headers={**WORKER, "x-vertep-token": "wrong"})
    assert response.status_code == 401
    assert response.text != "Authentication required"  # the route's own check answered


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/workers/model-progress"),        # other method of a machine path
    ("GET", "/api/nodes/w1/self-test"),
    ("GET", "/api/tasks/queue"),                   # admin route next to the machine ones
    ("GET", "/api/nodes"),
])
def test_other_routes_stay_behind_the_session(client, method, path):
    response = client.request(method, path, headers=WORKER)
    assert response.status_code == 401
    assert response.text == "Authentication required"
