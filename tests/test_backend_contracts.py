import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.configuration import CharacterConfig, generate_character_id, load_character, save_character
from core.state import store
from core.workflows import WorkflowRegistry, validate_workflow


client = TestClient(app)


class TestCharacterIDGeneration:
    def test_generate_character_id_format(self):
        cid = generate_character_id()
        assert len(cid) == 12
        assert cid.islower()
        assert cid.isalnum()

    def test_generate_character_id_unique(self):
        ids = {generate_character_id() for _ in range(100)}
        assert len(ids) == 100

    def test_character_config_auto_generates_id(self):
        config = CharacterConfig(name="Test Character")
        assert config.id is not None
        assert len(config.id) == 12
        assert config.id.islower()
        assert config.id.isalnum()

    def test_character_config_accepts_explicit_id(self):
        config = CharacterConfig(id="custom_id_123", name="Test Character")
        assert config.id == "custom_id_123"

    def test_character_config_rejects_invalid_id(self):
        with pytest.raises(ValueError):
            CharacterConfig(id="Invalid ID!", name="Test")


class TestCharacterCreateAPI:
    def test_create_character_auto_id(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            resp = client.post("/api/characters", json={"name": "Auto ID Character"})
            assert resp.status_code == 200
            data = resp.json()
            assert "id" in data
            assert len(data["id"]) == 12
            assert data["id"].islower()
            assert data["name"] == "Auto ID Character"
            assert (char_root / data["id"] / "character.json").exists()
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)

    def test_create_character_explicit_id_backward_compat(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            resp = client.post("/api/characters", json={"id": "my_custom_char", "name": "Explicit ID"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "my_custom_char"
            assert data["name"] == "Explicit ID"
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)

    def test_create_character_rejects_invalid_id(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            resp = client.post("/api/characters", json={"id": "Invalid ID!", "name": "Bad"})
            assert resp.status_code in (400, 422)
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)

    def test_create_character_conflict(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            client.post("/api/characters", json={"id": "existing_char", "name": "First"})
            resp = client.post("/api/characters", json={"id": "existing_char", "name": "Second"})
            assert resp.status_code == 409
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)


class TestCharacterPutAPI:
    def test_put_character_forbids_id_change(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            client.post("/api/characters", json={"id": "test_char", "name": "Original"})
            resp = client.put("/api/characters/test_char", json={"id": "different_id", "name": "Changed"})
            assert resp.status_code == 400
            assert "cannot be changed" in resp.json()["detail"].lower()
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)

    def test_put_character_allows_other_changes(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            client.post("/api/characters", json={"id": "test_char", "name": "Original"})
            resp = client.put("/api/characters/test_char", json={"id": "test_char", "name": "Updated", "enabled": False})
            assert resp.status_code == 200
            assert resp.json()["name"] == "Updated"
            assert resp.json()["enabled"] is False
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)


class TestCharacterDeleteAPI:
    def test_delete_character_structured_409(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        job_root = tmp_path / "jobs"
        os.environ["JOB_ROOT"] = str(job_root)
        try:
            client.post("/api/characters", json={"id": "referenced_char", "name": "Referenced"})
            from core.models import Job, JobStatus
            job = Job(job_id="2026-test1", topic="Test", character_id="referenced_char", priority=5,
                      status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00")
            store.jobs[job.job_id] = job

            resp = client.delete("/api/characters/referenced_char")
            assert resp.status_code == 409
            assert "X-Dependencies" in resp.headers
            deps = json.loads(resp.headers["X-Dependencies"])
            assert "jobs" in deps
            assert len(deps["jobs"]) == 1
            assert deps["jobs"][0]["job_id"] == "2026-test1"
            del store.jobs[job.job_id]
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)
            os.environ.pop("JOB_ROOT", None)

    def test_delete_character_success_when_no_deps(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        try:
            client.post("/api/characters", json={"id": "standalone_char", "name": "Standalone"})
            resp = client.delete("/api/characters/standalone_char")
            assert resp.status_code == 200
            assert resp.json()["deleted"] == "standalone_char"
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)


class TestCharacterUsageAPI:
    def test_character_usage_endpoint(self, tmp_path):
        char_root = tmp_path / "characters"
        os.environ["CHARACTERS_ROOT"] = str(char_root)
        job_root = tmp_path / "jobs"
        os.environ["JOB_ROOT"] = str(job_root)
        try:
            client.post("/api/characters", json={"id": "usage_char", "name": "Usage Test"})
            from core.models import Job, JobStatus
            job1 = Job(job_id="2026-usage1", topic="Job 1", character_id="usage_char", priority=5,
                       status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00")
            job2 = Job(job_id="2026-usage2", topic="Job 2", character_id="usage_char", priority=5,
                       status=JobStatus.READY, created_at="2026-01-02T00:00:00+00:00")
            store.jobs[job1.job_id] = job1
            store.jobs[job2.job_id] = job2

            resp = client.get("/api/characters/usage_char/usage")
            assert resp.status_code == 200
            data = resp.json()
            assert data["character_id"] == "usage_char"
            assert data["total_jobs"] == 2
            assert len(data["jobs"]) == 2
            del store.jobs[job1.job_id]
            del store.jobs[job2.job_id]
        finally:
            os.environ.pop("CHARACTERS_ROOT", None)
            os.environ.pop("JOB_ROOT", None)


class TestWorkflowValidation:
    def test_validate_workflow_valid(self):
        workflow = {
            "1": {"class_type": "KSampler", "inputs": {"seed": 123}},
            "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test"}},
        }
        result = validate_workflow(workflow)
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["schema"]["node_count"] == 2
        assert "KSampler" in result["schema"]["node_types"]

    def test_validate_workflow_invalid_empty(self):
        result = validate_workflow({})
        assert result["valid"] is False
        assert "non-empty object" in result["errors"][0]

    def test_validate_workflow_invalid_node_not_object(self):
        workflow = {"1": "not an object"}
        result = validate_workflow(workflow)
        assert result["valid"] is False
        assert "must be an object" in result["errors"][0]

    def test_validate_workflow_missing_class_type(self):
        workflow = {"1": {"inputs": {}}}
        result = validate_workflow(workflow)
        assert result["valid"] is False
        assert "no class_type" in result["errors"][0]

    def test_validate_workflow_unsupported_placeholder(self):
        workflow = {"1": {"class_type": "Test", "inputs": {"prompt": "{{UNKNOWN}}"}}}
        result = validate_workflow(workflow)
        assert result["valid"] is False
        assert "Unsupported placeholder: UNKNOWN" in result["errors"][0]

    def test_validate_workflow_supported_placeholders(self):
        workflow = {"1": {"class_type": "Test", "inputs": {"width": "{{WIDTH}}", "height": "{{HEIGHT}}"}}}
        result = validate_workflow(workflow)
        assert result["valid"] is True
        assert "WIDTH" in result["schema"]["has_placeholders"]
        assert "HEIGHT" in result["schema"]["has_placeholders"]

    def test_validate_workflow_warnings(self):
        workflow = {str(i): {"class_type": "Node", "inputs": {}} for i in range(55)}
        result = validate_workflow(workflow)
        assert result["valid"] is True
        assert any("Large workflow" in w for w in result["warnings"])


class TestWorkflowRegistry:
    def test_save_and_load_with_validation(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        result = registry.save("image", "test.json", workflow)
        assert result["valid"] is True
        assert "validation" in result
        assert result["validation"]["valid"] is True

        loaded = registry.load("image", "test.json")
        assert "workflow" in loaded
        assert "validation" in loaded
        assert loaded["validation"]["valid"] is True

    def test_save_rejects_invalid_workflow(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow = {"1": {"inputs": {}}}
        with pytest.raises(ValueError) as exc:
            registry.save("image", "invalid.json", workflow)
        assert "no class_type" in str(exc.value)

    def test_save_protects_existing_valid_workflow(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}
        registry.save("image", "protected.json", workflow1)

        with pytest.raises(ValueError) as exc:
            registry.save("image", "protected.json", workflow2)
        assert "already exists and is valid" in str(exc.value)
        assert "force=true" in str(exc.value)

    def test_save_with_force_overwrites(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}
        registry.save("image", "forced.json", workflow1)
        result = registry.save("image", "forced.json", workflow2, force=True)
        assert result["valid"] is True

        loaded = registry.load("image", "forced.json")
        assert loaded["workflow"]["1"]["inputs"]["seed"] == 456

    def test_version_history_created_on_overwrite(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}
        registry.save("image", "versioned.json", workflow1)
        registry.save("image", "versioned.json", workflow2, force=True)

        versions = registry.get_versions("image", "versioned.json")
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert versions[0]["deleted"] is False

    def test_load_version(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}
        registry.save("image", "versioned.json", workflow1)
        registry.save("image", "versioned.json", workflow2, force=True)

        v1 = registry.load_version("image", "versioned.json", 1)
        assert v1["workflow"]["1"]["inputs"]["seed"] == 123
        assert v1["validation"]["valid"] is True

    def test_restore_version(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}
        registry.save("image", "versioned.json", workflow1)
        registry.save("image", "versioned.json", workflow2, force=True)

        result = registry.restore_version("image", "versioned.json", 1)
        assert result["valid"] is True

        loaded = registry.load("image", "versioned.json")
        assert loaded["workflow"]["1"]["inputs"]["seed"] == 123

    def test_delete_with_dependencies_returns_structured(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        registry.save("image", "referenced.json", workflow)

        from core.models import Job, JobStatus
        job_root = tmp_path / "jobs"
        os.environ["JOB_ROOT"] = str(job_root)
        job = Job(job_id="2026-wf1", topic="WF Job", character_id="char", priority=5,
                  status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00",
                  workflow="workflows/image/referenced.json")
        store.jobs[job.job_id] = job

        try:
            with pytest.raises(ValueError) as exc:
                registry.delete("image", "referenced.json")
            assert "referenced by other resources" in str(exc.value)
        finally:
            del store.jobs[job.job_id]
            os.environ.pop("JOB_ROOT", None)

    def test_delete_with_force_removes(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
        registry.save("image", "forced_delete.json", workflow)

        result = registry.delete("image", "forced_delete.json", force=True)
        assert result["deleted"] == "workflows/image/forced_delete.json"
        assert result["archived"] is True

    def test_form_schema_endpoint(self, tmp_path):
        registry = WorkflowRegistry(tmp_path)
        workflow = {
            "1": {"class_type": "KSampler", "inputs": {"seed": 123, "steps": 20, "cfg": 7.0}},
            "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "output"}},
        }
        registry.save("image", "form_test.json", workflow)

        form = registry.get_form_schema("image", "form_test.json")
        assert form["type"] == "image"
        assert form["name"] == "form_test.json"
        assert form["validation"]["valid"] is True
        assert len(form["form_fields"]) > 0
        field = form["form_fields"][0]
        assert "node_id" in field
        assert "input_name" in field
        assert "current_value" in field
        assert "type" in field
        assert "class_type" in field


class TestWorkflowAPI:
    def _setup_workflow_registry(self, tmp_path):
        from core.state import workflow_registry as global_registry
        from core.workflows import WorkflowRegistry
        wf_root = tmp_path / "workflows"
        new_registry = WorkflowRegistry(wf_root)
        # Monkey-patch the global registry
        import core.state
        core.state.workflow_registry = new_registry
        return new_registry

    def test_put_workflow_force_parameter(self, tmp_path):
        self._setup_workflow_registry(tmp_path)
        try:
            workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
            workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}

            resp1 = client.put("/api/workflows/image/api_test.json", json=workflow1)
            assert resp1.status_code == 200

            resp2 = client.put("/api/workflows/image/api_test.json", json=workflow2)
            assert resp2.status_code == 400
            assert "already exists" in resp2.json()["detail"]

            resp3 = client.put("/api/workflows/image/api_test.json", json=workflow2, params={"force": "true"})
            assert resp3.status_code == 200
        finally:
            # Reset to default
            from core.state import workflow_registry
            import core.state
            core.state.workflow_registry = WorkflowRegistry()

    def test_delete_workflow_force_parameter(self, tmp_path):
        self._setup_workflow_registry(tmp_path)
        try:
            workflow = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
            client.put("/api/workflows/image/delete_test.json", json=workflow)

            resp = client.delete("/api/workflows/image/delete_test.json")
            assert resp.status_code == 200
        finally:
            from core.state import workflow_registry
            import core.state
            core.state.workflow_registry = WorkflowRegistry()

    def test_workflow_versions_endpoint(self, tmp_path):
        self._setup_workflow_registry(tmp_path)
        try:
            workflow1 = {"1": {"class_type": "KSampler", "inputs": {"seed": 123}}}
            workflow2 = {"1": {"class_type": "KSampler", "inputs": {"seed": 456}}}
            client.put("/api/workflows/image/version_api.json", json=workflow1)
            client.put("/api/workflows/image/version_api.json", json=workflow2, params={"force": "true"})

            resp = client.get("/api/workflows/image/version_api.json/versions")
            assert resp.status_code == 200
            versions = resp.json()
            assert len(versions) == 1
            assert versions[0]["version"] == 1
        finally:
            from core.state import workflow_registry
            import core.state
            core.state.workflow_registry = WorkflowRegistry()

    def test_workflow_form_endpoint(self, tmp_path):
        self._setup_workflow_registry(tmp_path)
        try:
            workflow = {"1": {"class_type": "KSampler", "inputs": {"seed": 123, "steps": 20}}}
            client.put("/api/workflows/image/form_api.json", json=workflow)

            resp = client.get("/api/workflows/image/form_api.json/form")
            assert resp.status_code == 200
            form = resp.json()
            assert form["type"] == "image"
            assert form["name"] == "form_api.json"
            assert "form_fields" in form
            assert len(form["form_fields"]) > 0
        finally:
            from core.state import workflow_registry
            import core.state
            core.state.workflow_registry = WorkflowRegistry()
class TestRBACAuthorization:
    """Issue #29: RBAC UX — viewer must never perform admin mutations,
    while profile/password/logout stay available without shell access."""

    def _auth(self, monkeypatch, user, role, password="rbac-fallback-password"):
        monkeypatch.setenv("ADMIN_PASSWORD", password)
        monkeypatch.delenv("CONFIG_ROOT", raising=False)
        import hashlib
        import hmac
        from core.security import _session_token
        token = _session_token(user, role)
        csrf = hmac.new(password.encode(), token.encode(), hashlib.sha256).hexdigest()
        return ({"vertep_session": token, "vertep_csrf": csrf},
                {"x-csrf-token": csrf})

    def test_viewer_forbidden_admin_mutation(self, monkeypatch):
        cookies, headers = self._auth(monkeypatch, "reader", "viewer")
        monkeypatch.setenv("USERS_JSON", json.dumps(
            {"reader": {"password_hash": "x", "role": "viewer"}}))
        resp = client.delete("/api/characters/some_char", cookies=cookies, headers=headers)
        assert resp.status_code == 403
        assert "Insufficient role" in resp.text

    def test_viewer_forbidden_job_create(self, monkeypatch):
        cookies, headers = self._auth(monkeypatch, "reader", "viewer")
        resp = client.post("/api/jobs", json={"topic": "blocked"}, cookies=cookies, headers=headers)
        assert resp.status_code == 403

    def test_viewer_cannot_mutate_settings(self, monkeypatch):
        cookies, headers = self._auth(monkeypatch, "reader", "viewer")
        resp = client.post("/api/settings/logo", cookies=cookies, headers=headers)
        assert resp.status_code == 403

    def test_viewer_read_allowed(self, monkeypatch):
        cookies, _ = self._auth(monkeypatch, "reader", "viewer")
        monkeypatch.setenv("USERS_JSON", json.dumps(
            {"reader": {"password_hash": "x", "role": "viewer"}}))
        resp = client.get("/api/session", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["role"] == "viewer"

    def test_viewer_can_logout(self, monkeypatch):
        cookies, _ = self._auth(monkeypatch, "reader", "viewer")
        resp = client.delete("/api/session", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    def test_viewer_change_password_reaches_validation(self, monkeypatch):
        cookies, headers = self._auth(monkeypatch, "reader", "viewer")
        resp = client.put("/api/session/password",
                          json={"old_password": "old", "new_password": "short"},
                          cookies=cookies, headers=headers)
        # Not role-blocked (403): security validation runs.
        assert resp.status_code == 400
        assert "12 characters" in resp.json()["detail"]

    def test_viewer_change_password_wrong_old_rejected(self, monkeypatch):
        cookies, headers = self._auth(monkeypatch, "reader", "viewer")
        import hashlib
        import hmac
        from core.security import _hash_secret, _session_token
        token = _session_token("reader", "viewer")
        csrf = hmac.new("rbac-fallback-password".encode(), token.encode(),
                        hashlib.sha256).hexdigest()
        cookies = {"vertep_session": token, "vertep_csrf": csrf}
        headers = {"x-csrf-token": csrf}
        monkeypatch.setenv("USERS_JSON", json.dumps({
            "reader": {"password_hash": _hash_secret("correct-old"), "role": "viewer"}}))
        resp = client.put("/api/session/password",
                          json={"old_password": "wrong-old", "new_password": "newlongpassword12"},
                          cookies=cookies, headers=headers)
        assert resp.status_code == 400
        assert "incorrect" in resp.json()["detail"]

    def test_admin_mutation_requires_csrf(self, monkeypatch):
        cookies, _ = self._auth(monkeypatch, "admin", "admin")
        resp = client.delete("/api/characters/some_char", cookies=cookies)
        assert resp.status_code == 403
        assert "CSRF" in resp.text

    def test_unauthenticated_returns_401(self, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "protected-env-secret")
        monkeypatch.delenv("CONFIG_ROOT", raising=False)
        resp = client.get("/api/jobs")
        assert resp.status_code == 401