"""Issue #114/#168.3/#168.4: table-driven UI/API system-state mutation matrix.

The CORE middleware classifies every non-GET request into a coarse operation
(``create_job``, ``mutate_job``, ``node_control``, ``configuration``, ``update``,
``backup``, ``restore``, ``model_pull``, ``resource_management``, ``recovery``)
and the central ``STATE_OPERATION_POLICY`` (core/system_state.py) decides whether
that operation is admitted in the current system state. This test pins the
concrete route -> operation classification against that policy so the
table-driven UI (``web-v2`` ``PolicyService``) and the API cannot drift, and so
the restore-before-backup ordering regression cannot return.

Each probe uses a path that matches the intended classification prefix but that
does not perform destructive work: a blocked state must answer 423 before the
route is reached, and an admitted state must simply not answer 423.
"""

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.system_state import STATE_OPERATION_POLICY, SystemState, get_system_state, set_system_state

client = TestClient(app, raise_server_exceptions=False)

# (label, expected operation, method, path, json body)
PROBES = [
    ("create_job", "create_job", "POST", "/api/jobs", {"topic": "matrix"}),
    ("mutate_job", "mutate_job", "POST", "/api/jobs/missing/pause", {}),
    ("node_control", "node_control", "POST", "/api/nodes/missing/actions", {"action": "restart"}),
    ("resource_characters", "resource_management", "POST", "/api/characters", {"character_id": "matrix"}),
    ("resource_brands", "resource_management", "POST", "/api/brands", {}),
    ("resource_workflows", "resource_management", "PUT", "/api/workflows/image/matrix", {}),
    ("resource_channels", "resource_management", "DELETE", "/api/channels/missing", None),
    ("configuration", "configuration", "POST", "/api/settings/probe", {}),
    ("update", "update", "POST", "/api/system/update/probe", {}),
    ("backup", "backup", "POST", "/api/system/backups/probe", {}),
    ("restore", "restore", "POST", "/api/system/backups/snap-1/restore", {}),
    ("model_pull_system", "model_pull", "POST", "/api/system/models/pull/probe", {}),
    ("model_delete_system", "model_pull", "DELETE", "/api/system/models/probe", None),
    ("model_pull_text", "model_pull", "POST", "/api/models/text/pull", {"name": "probe"}),
    ("model_delete_text", "model_pull", "DELETE", "/api/models/text/probe", None),
    ("recovery", "recovery", "POST", "/api/system/recovery/probe", {}),
]

STATES = [SystemState.NORMAL, SystemState.MAINTENANCE, SystemState.UPDATING,
          SystemState.RECOVERING, SystemState.READ_ONLY, SystemState.EMERGENCY]


def _call(method: str, path: str, body):
    if method == "DELETE":
        return client.delete(path)
    return client.request(method, path, json=body)


@pytest.mark.parametrize("state", STATES, ids=[s.value for s in STATES])
@pytest.mark.parametrize("label,operation,method,path,body", PROBES,
                         ids=[probe[0] for probe in PROBES])
def test_route_classification_matches_state_policy(state, label, operation, method, path, body,
                                                   monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    previous = get_system_state()["state"]
    try:
        set_system_state(state, f"matrix {state.value}")
        response = _call(method, path, body)
        allowed = operation in STATE_OPERATION_POLICY[state]
        if allowed:
            assert response.status_code != 423, (
                f"{label} ({operation}) must be admitted in {state.value} "
                f"but was blocked: {response.text}")
        else:
            assert response.status_code == 423, (
                f"{label} ({operation}) must be blocked in {state.value} "
                f"but returned {response.status_code}")
            assert "blocked by system state" in response.text
            assert operation in response.text
    finally:
        set_system_state(previous, "matrix restore")


def test_restore_is_not_misclassified_as_backup(monkeypatch, tmp_path):
    """Issue #114: the general POST /api/system/backups branch used to precede
    /restore, so a restore request was classified as ``backup`` and wrongly
    blocked in RECOVERING (where the policy admits ``restore``)."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.RECOVERING, "matrix recovering")
        response = client.post("/api/system/backups/snap-1/restore", json={})
        assert response.status_code != 423, (
            f"restore must be admitted in RECOVERING, got {response.status_code}: {response.text}")
        assert "restore" not in response.text or "blocked by system state" not in response.text
    finally:
        set_system_state(previous, "matrix restore")


# ---------------------------------------------------------------------------
# Issue #168.2/#168.4: the actual-backend role x state x resource matrix
#
# The table above pins the route -> operation classification against the
# central policy.  The cells that were still missing are the ones that need a
# real request against the real application: a forbidden mutation (blocked
# system state or the viewer role) must leave the resource exactly as it was,
# the allowed recovery/session operations must keep working, and the
# method/path contract of every remaining probe is pinned by an executable
# request instead of a source-string check.
# ---------------------------------------------------------------------------

ADMIN_PASSWORD = "matrix-admin-password"
VIEWER_PASSWORD = "matrix-viewer-password"

CHARACTER_BODY = {"id": "matrix_char", "name": "Matrix Character"}
BRAND_BODY = {"id": "matrix_brand", "name": "Matrix Brand"}
WORKFLOW_BODY = {"1": {"class_type": "SaveImage", "inputs": {}}}


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def actual_backend(monkeypatch, tmp_path):
    """A configured installation with isolated resource roots and a viewer account."""
    import core.state
    from core.security import _hash_secret, _session_token
    from core.workflows import WorkflowRegistry

    config = tmp_path / "config"
    characters = tmp_path / "characters"
    brands = tmp_path / "brands"
    workflows = tmp_path / "workflows"
    for directory in (config, characters, brands):
        directory.mkdir(parents=True, exist_ok=True)
    (config / "installation.json").write_text(
        json.dumps({"installation_id": "matrix", "completed_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8")
    (workflows / "image").mkdir(parents=True, exist_ok=True)
    (workflows / "image" / "demo.json").write_text(json.dumps(WORKFLOW_BODY), encoding="utf-8")

    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    monkeypatch.setenv("CHARACTERS_ROOT", str(characters))
    monkeypatch.setenv("BRANDS_ROOT", str(brands))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("USERS_JSON", json.dumps({
        "reader": {"password_hash": _hash_secret(VIEWER_PASSWORD), "role": "viewer"}}))

    original_registry = core.state.workflow_registry
    core.state.workflow_registry = WorkflowRegistry(workflows)
    token = _session_token("reader", "viewer")
    csrf = hmac.new(ADMIN_PASSWORD.encode(), token.encode(), hashlib.sha256).hexdigest()
    try:
        yield {
            "client": TestClient(app, raise_server_exceptions=False),
            "admin": _basic("admin", ADMIN_PASSWORD),
            "viewer_cookies": {"vertep_session": token, "vertep_csrf": csrf},
            "viewer_headers": {"x-csrf-token": csrf},
            "characters": characters,
            "brands": brands,
            "workflows": workflows,
        }
    finally:
        core.state.workflow_registry = original_registry


def _request(ctx, method: str, path: str, body, *, role: str = "admin"):
    headers = ctx["admin"] if role == "admin" else ctx["viewer_headers"]
    cookies = None if role == "admin" else ctx["viewer_cookies"]
    if method == "DELETE":
        return ctx["client"].delete(path, headers=headers, cookies=cookies)
    return ctx["client"].request(method, path, json=body, headers=headers, cookies=cookies)


def _character_ids(ctx) -> list[str]:
    return sorted(path.name for path in ctx["characters"].iterdir())


def _brand_ids(ctx) -> list[str]:
    return sorted(path.name for path in ctx["brands"].iterdir())


def _workflow_files(ctx) -> list[str]:
    return sorted(path.relative_to(ctx["workflows"]).as_posix()
                  for path in ctx["workflows"].rglob("*.json"))


def _text_models(ctx) -> str:
    response = ctx["client"].get("/api/models/text", headers=ctx["admin"])
    return response.text


def _channel_ids(ctx) -> list[str]:
    from core.state import store

    return sorted(channel.channel_id for channel in store.repository.list_channels())


def _prepare_channel(ctx) -> str:
    created_brand = ctx["client"].post("/api/brands", json=BRAND_BODY, headers=ctx["admin"])
    assert created_brand.status_code == 200, created_brand.text
    created_channel = ctx["client"].post(
        "/api/brands/matrix_brand/channels",
        json={"brand_id": "matrix_brand", "channel_type": "telegram",
              "target": "@matrix_channel"},
        headers=ctx["admin"])
    assert created_channel.status_code == 200, created_channel.text
    return created_channel.json()["channel_id"]


def _probe_characters(ctx) -> dict:
    return {
        "method": "POST",
        "path": "/api/characters",
        "body": CHARACTER_BODY,
        "snapshot": lambda: _character_ids(ctx),
        "changed": lambda before, after: "matrix_char" not in before and "matrix_char" in after,
    }


def _probe_brands(ctx) -> dict:
    return {
        "method": "POST",
        "path": "/api/brands",
        "body": BRAND_BODY,
        "snapshot": lambda: _brand_ids(ctx),
        "changed": lambda before, after: "matrix_brand" not in before and "matrix_brand" in after,
    }


def _probe_workflows(ctx) -> dict:
    return {
        "method": "PUT",
        "path": "/api/workflows/image/matrix.json",
        "body": WORKFLOW_BODY,
        "snapshot": lambda: _workflow_files(ctx),
        "changed": lambda before, after: "image/matrix.json" in after
        and "image/matrix.json" not in before,
    }


def _probe_models(ctx) -> dict:
    return {
        "method": "POST",
        "path": "/api/models/text/pull",
        "body": {"name": "matrix-model"},
        "snapshot": lambda: _text_models(ctx),
        "changed": None,
    }


def _probe_channels(ctx) -> dict:
    channel_id = _prepare_channel(ctx)
    return {
        "method": "DELETE",
        "path": f"/api/channels/{channel_id}",
        "body": None,
        "snapshot": lambda: _channel_ids(ctx),
        "changed": lambda before, after: channel_id in before and channel_id not in after,
    }


PROBES = {
    "characters": _probe_characters,
    "brands": _probe_brands,
    "channels": _probe_channels,
    "models": _probe_models,
    "workflows": _probe_workflows,
}


@pytest.mark.parametrize("label", sorted(PROBES))
def test_a_blocked_state_leaves_the_resource_unchanged(label, actual_backend, monkeypatch, tmp_path):
    """Issue #168.4: a forbidden mutation must not change the resource."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    probe = PROBES[label](actual_backend)
    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.READ_ONLY, "matrix read only")
        before = probe["snapshot"]()
        response = _request(actual_backend, probe["method"], probe["path"], probe["body"])
        assert response.status_code == 423, response.text
        assert "blocked by system state" in response.text
        assert probe["snapshot"]() == before
    finally:
        set_system_state(previous, "matrix restore")


@pytest.mark.parametrize("label", sorted(PROBES))
def test_a_viewer_mutation_is_refused_and_leaves_the_resource_unchanged(
        label, actual_backend, monkeypatch, tmp_path):
    """Issue #168.4: the viewer role is refused by the API, not only by the UI."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    probe = PROBES[label](actual_backend)
    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.NORMAL, "matrix normal")
        before = probe["snapshot"]()
        response = _request(actual_backend, probe["method"], probe["path"], probe["body"],
                            role="viewer")
        assert response.status_code == 403, response.text
        assert "Insufficient role" in response.text
        assert probe["snapshot"]() == before
    finally:
        set_system_state(previous, "matrix restore")


@pytest.mark.parametrize("label", ["brands", "characters", "channels", "workflows"])
def test_the_administrator_probe_changes_the_resource_it_guards(label, actual_backend,
                                                                monkeypatch, tmp_path):
    """Positive control: the before/after assertions above are meaningful."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    probe = PROBES[label](actual_backend)
    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.NORMAL, "matrix normal")
        before = probe["snapshot"]()
        response = _request(actual_backend, probe["method"], probe["path"], probe["body"])
        assert response.status_code == 200, response.text
        after = probe["snapshot"]()
        assert probe["changed"](before, after)
    finally:
        set_system_state(previous, "matrix restore")


def test_the_model_pull_probe_keeps_its_method_and_path(actual_backend, monkeypatch, tmp_path):
    """Issue #168.2: the guarded route keeps answering on its own method and path."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    probe = _probe_models(actual_backend)
    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.NORMAL, "matrix normal")
        response = _request(actual_backend, probe["method"], probe["path"], probe["body"])
        assert response.status_code not in {404, 405}, response.text
    finally:
        set_system_state(previous, "matrix restore")


@pytest.mark.parametrize("state", [SystemState.MAINTENANCE, SystemState.UPDATING,
                                   SystemState.RECOVERING])
def test_create_job_is_accepted_and_waits_for_the_system(state, actual_backend,
                                                          monkeypatch, tmp_path):
    """Issue #168.3: the agreed states accept the job with WAITING_FOR_SYSTEM."""
    from core.state import store

    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    created = actual_backend["client"].post("/api/characters", json=CHARACTER_BODY,
                                            headers=actual_backend["admin"])
    assert created.status_code == 200, created.text
    previous = get_system_state()["state"]
    job_id = None
    try:
        set_system_state(state, f"matrix {state.value}")
        response = actual_backend["client"].post(
            "/api/jobs", json={"topic": "matrix", "character_id": "matrix_char"},
            headers=actual_backend["admin"])
        assert response.status_code == 200, response.text
        body = response.json()
        job_id = body["job_id"]
        assert body["status"] == "WAITING_FOR_SYSTEM"
    finally:
        set_system_state(previous, "matrix restore")
        if job_id:
            store.delete(job_id)


@pytest.mark.parametrize("state", [SystemState.READ_ONLY, SystemState.EMERGENCY])
def test_create_job_is_blocked_and_creates_no_job(state, actual_backend, monkeypatch, tmp_path):
    """Issue #168.3: a blocked state answers 423 and creates nothing."""
    from core.state import store

    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    created = actual_backend["client"].post("/api/characters", json=CHARACTER_BODY,
                                            headers=actual_backend["admin"])
    assert created.status_code == 200, created.text
    previous = get_system_state()["state"]
    try:
        set_system_state(state, f"matrix {state.value}")
        before = len(store.jobs)
        response = actual_backend["client"].post(
            "/api/jobs", json={"topic": "matrix", "character_id": "matrix_char"},
            headers=actual_backend["admin"])
        assert response.status_code == 423, response.text
        assert "blocked by system state" in response.text
        assert len(store.jobs) == before
    finally:
        set_system_state(previous, "matrix restore")


@pytest.mark.parametrize("state", [SystemState.MAINTENANCE, SystemState.RECOVERING,
                                   SystemState.READ_ONLY, SystemState.EMERGENCY])
def test_viewer_session_operations_keep_working_in_a_restricted_state(
        state, actual_backend, monkeypatch, tmp_path):
    """Issue #168.3: session operations stay available in every restricted state."""
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    previous = get_system_state()["state"]
    try:
        set_system_state(state, f"matrix {state.value}")
        session = actual_backend["client"].get("/api/session",
                                                cookies=actual_backend["viewer_cookies"])
        assert session.status_code == 200, session.text
        assert session.json()["authenticated"] is True
        assert session.json()["role"] == "viewer"

        profile = actual_backend["client"].put(
            "/api/session/profile", json={"display_name": "Matrix Reader"},
            cookies=actual_backend["viewer_cookies"], headers=actual_backend["viewer_headers"])
        assert profile.status_code == 200, profile.text
        assert profile.json()["display_name"] == "Matrix Reader"

        logout = actual_backend["client"].delete("/api/session",
                                                  cookies=actual_backend["viewer_cookies"])
        assert logout.status_code == 200, logout.text
        assert logout.json()["authenticated"] is False
    finally:
        set_system_state(previous, "matrix restore")
