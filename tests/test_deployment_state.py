"""A FAILED deployment of an older Vertep version is history after an update (20.11.2026).

On the stand a request made during the 0.0.4.17 → 0.0.4.22 update was refused with
"Запит на розгортання створено для іншої версії Vertep"; afterwards the Modules page kept
showing "Застосування не вдалося" and the ROLE_DEPLOYMENT_FAILED alert stayed active.
"""

from core.deployment_state import VERSION_MISMATCH, stale_failure


def test_a_failure_of_another_version_is_stale_and_a_current_one_is_not():
    assert stale_failure({"state": "FAILED", "version": "0.0.4.17", "error": "x"}, "0.0.4.23")
    assert not stale_failure({"state": "FAILED", "version": "0.0.4.23", "error": "x"}, "0.0.4.23")
    assert not stale_failure({"state": "SUCCEEDED", "version": "0.0.4.17"}, "0.0.4.23")


def test_legacy_status_without_version_is_stale_only_for_the_version_mismatch():
    assert stale_failure({"state": "FAILED", "error": VERSION_MISMATCH}, "0.0.4.23")
    assert not stale_failure({"state": "FAILED", "error": "docker compose up failed"}, "0.0.4.23")


def test_roles_endpoint_hides_a_stale_failure(monkeypatch, tmp_path):
    import json
    from fastapi.testclient import TestClient
    from core.app import app
    monkeypatch.setattr("core.api.system.config_root", lambda: tmp_path)
    monkeypatch.setattr("core.version.application_version", lambda: "0.0.4.23")
    (tmp_path / "deployment-status.json").write_text(json.dumps(
        {"state": "FAILED", "phase": "VALIDATING", "error": VERSION_MISMATCH, "updated_at": "2026-11-20T17:59:33+00:00"}))
    body = TestClient(app).get("/api/system/roles").json()
    assert body["deployment"].get("state") != "FAILED"
