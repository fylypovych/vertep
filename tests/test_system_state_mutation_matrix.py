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
