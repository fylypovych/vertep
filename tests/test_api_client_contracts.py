"""Contract tests for the Web UI V2 typed domain API clients (issue i.0.0.0.23).

Ensures that every endpoint referenced by the typed Angular domain clients
(Queue, Publishing, Storyboard, Operations, Setup, Session) actually exists on
the backend with the expected HTTP method, so frontend request-mapping stays in
sync with the API surface without a running browser. A couple of safe read-only
endpoints are also smoke-tested for their typed shape.
"""
from fastapi.testclient import TestClient

from core.app import app


def _client():
    return TestClient(app, raise_server_exceptions=False)


def _route_table():
    """Map normalized (METHOD, path-template) -> True from the FastAPI app."""
    table = set()
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            table.add((method.upper(), route.path))
    return table


def _assert_endpoints(pairs):
    table = _route_table()
    for method, path in pairs:
        assert (method, path) in table, f"Missing backend route {method} {path}"


def test_queue_client_endpoints_exist():
    _assert_endpoints([
        ("GET", "/api/tasks/queue"),
        ("GET", "/api/tasks/dead-letter"),
        ("POST", "/api/tasks/dead-letter/{task_id}/retry"),
    ])


def test_publishing_client_endpoints_exist():
    _assert_endpoints([
        ("POST", "/api/jobs/{job_id}/publish"),
    ])


def test_storyboard_client_endpoints_exist():
    _assert_endpoints([
        ("GET", "/api/jobs/{job_id}/storyboards"),
        ("GET", "/api/jobs/{job_id}/storyboards/{version}"),
        ("POST", "/api/jobs/{job_id}/storyboards/generate"),
        ("POST", "/api/jobs/{job_id}/storyboards/approve"),
        ("POST", "/api/jobs/{job_id}/storyboards/reject"),
        ("POST", "/api/jobs/{job_id}/storyboards/regenerate"),
        ("POST", "/api/jobs/{job_id}/storyboards/images/approve"),
        ("POST", "/api/jobs/{job_id}/storyboards/images/revision"),
        ("POST", "/api/jobs/{job_id}/storyboards/images/regenerate"),
    ])


def test_operations_client_endpoints_exist():
    _assert_endpoints([
        ("GET", "/api/alerts"),
        ("GET", "/api/logs"),
        ("GET", "/api/metrics"),
        ("GET", "/api/security/check"),
        ("GET", "/api/health"),
        ("GET", "/api/health/history"),
        ("GET", "/api/system/backups"),
        ("POST", "/api/system/backups"),
        ("POST", "/api/system/backups/{snapshot_id}/restore"),
        ("POST", "/api/system/recovery/normal"),
        ("GET", "/api/operations"),
        ("GET", "/api/operations/{operation_id}"),
        ("POST", "/api/system/restart"),
        ("POST", "/api/system/test"),
        ("GET", "/api/system/state"),
        ("POST", "/api/system/state"),
    ])


def test_setup_client_endpoints_exist():
    _assert_endpoints([
        ("GET", "/api/setup"),
        ("GET", "/api/setup/health"),
        ("POST", "/api/setup/complete"),
    ])


def test_session_client_endpoints_exist():
    _assert_endpoints([
        ("GET", "/api/session"),
        ("POST", "/api/session"),
        ("DELETE", "/api/session"),
        ("PUT", "/api/session/password"),
    ])


def test_operations_read_shapes():
    client = _client()
    # Alerts and metrics are safe read-only endpoints; verify the typed envelope.
    alerts = client.get("/api/alerts")
    assert alerts.status_code == 200
    assert isinstance(alerts.json(), list)

    metrics = client.get("/api/metrics")
    assert metrics.status_code == 200
    assert isinstance(metrics.json(), dict)