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
        # Issue #75 S3: the policy is reported by the server in the UI language.
        assert "12" in resp.json()["detail"]

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


class TestSessionIdentityContract:
    """Issue #75 S1: one validated identity decided by the server."""

    def _auth(self, monkeypatch, tmp_path, user, role, password="identity-contract-password"):
        monkeypatch.setenv("ADMIN_PASSWORD", password)
        monkeypatch.setenv("ADMIN_USER", "primary-admin")
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        (tmp_path / "installation.json").write_text(
            json.dumps({"installation_id": "test", "completed_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        import hashlib
        import hmac
        from core.security import _session_token
        token = _session_token(user, role)
        csrf = hmac.new(password.encode(), token.encode(), hashlib.sha256).hexdigest()
        return ({"vertep_session": token, "vertep_csrf": csrf},
                {"x-csrf-token": csrf})

    def test_invalid_credentials_never_open_a_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ADMIN_PASSWORD", "identity-contract-password")
        monkeypatch.setenv("ADMIN_USER", "primary-admin")
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        (tmp_path / "installation.json").write_text(
            json.dumps({"installation_id": "test", "completed_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        monkeypatch.setenv("USERS_JSON", "{}")
        import base64
        credentials = base64.b64encode(b"primary-admin:wrong-password").decode()
        resp = client.post("/api/session", headers={"Authorization": f"Basic {credentials}"})
        assert resp.status_code == 401
        assert "vertep_session" not in resp.cookies

    def test_utf8_basic_credentials_open_a_session(self, monkeypatch, tmp_path):
        # Issue #75 S1: Basic credentials are decoded as UTF-8 on both sides, so
        # a Cyrillic login/password must authenticate exactly as configured.
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        (tmp_path / "installation.json").write_text(
            json.dumps({"installation_id": "test", "completed_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        monkeypatch.setenv("USERS_JSON", "{}")
        login, password = "адміністратор", "пароль-ідентичності"
        monkeypatch.setenv("ADMIN_USER", login)
        monkeypatch.setenv("ADMIN_PASSWORD", password)
        import base64
        credentials = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
        # Окремий клієнт: успішний вхід не залишає session-cookie у спільній jar.
        # Без контекст-менеджера, щоб не запускати app lifespan у сусідніх тестах.
        utf8_client = TestClient(app)
        try:
            resp = utf8_client.post("/api/session", headers={"Authorization": f"Basic {credentials}"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["authenticated"] is True
            assert body["user"] == login
            assert body["role"] == "admin"
            assert utf8_client.get("/api/session").json()["user"] == login
        finally:
            utf8_client.cookies.clear()


    def test_configured_installation_rejects_anonymous_session_read(self, monkeypatch, tmp_path):
        cookies, _ = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        assert cookies
        # Middleware не пропускає анонімний запит, коли встановлено пароль.
        assert client.get("/api/session").status_code == 401

    def test_login_shell_is_public_while_api_stays_protected(self, monkeypatch, tmp_path):
        self._auth(monkeypatch, tmp_path, "reader", "viewer")
        # Issue #75 S1: без публічної оболонки сторінка /login не рендериться і
        # guard-redirect не має куди вести. Дані лишаються за автентифікацією.
        # На CI збірки web-v2/dist ще немає, тому 200 вимагається лише коли
        # скомпільований Angular UI присутній у checkout; головне — оболонка не
        # закрита автентифікацією.
        shell = client.get("/login")
        assert shell.status_code != 401, "/login is behind authentication"
        built_ui = any(Path(candidate).is_dir() for candidate in (
            "web-v2/dist/vertep-admin-v2", "web-v2/dist/vertep-admin-v2/browser"))
        if built_ui:
            assert shell.status_code == 200
        for path in ("/", "/v1/"):
            assert client.get(path).status_code == 200, f"{path} is not publicly reachable"
        for path in ("/api/session", "/api/jobs", "/api/characters"):
            assert client.get(path).status_code == 401, f"{path} is publicly readable"

    def test_unconfigured_installation_reports_explicitly_unauthenticated(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("USERS_JSON", raising=False)
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        (tmp_path / "installation.json").write_text(
            json.dumps({"installation_id": "test", "completed_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        resp = client.get("/api/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["role"] is None
        assert body["password_policy"]["min_length"] >= 12

    def test_session_reports_only_a_known_role(self, monkeypatch, tmp_path):
        from core.security import SESSION_ROLES
        cookies, _ = self._auth(monkeypatch, tmp_path, "reader", "superuser")
        body = client.get("/api/session", cookies=cookies).json()
        assert body["role"] in SESSION_ROLES
        # Невідома роль не підвищується до admin — найменш привілейована.
        assert body["role"] == "viewer"

    def test_session_exposes_canonical_profile_fields(self, monkeypatch, tmp_path):
        from core.security import _hash_secret
        monkeypatch.setenv("USERS_JSON", json.dumps({
            "reader": {"password_hash": _hash_secret("reader-secret"),
                       "role": "viewer", "display_name": " Reader ", "email": " reader@example.com "}}))
        cookies, _ = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        body = client.get("/api/session", cookies=cookies).json()
        assert body["authenticated"] is True
        assert body["user"] == "reader"
        assert body["login"] == "reader"
        assert body["display_name"] == "Reader"
        assert body["email"] == "reader@example.com"
        assert body["password_policy"] == {"min_length": 12, "require_different_from_current": True}

    def test_viewer_sees_the_same_contract_as_admin(self, monkeypatch, tmp_path):
        for user, role in (("reader", "viewer"), ("root-admin", "admin")):
            cookies, _ = self._auth(monkeypatch, tmp_path, user, role)
            body = client.get("/api/session", cookies=cookies).json()
            assert set(body) == {"authenticated", "user", "login", "role",
                                 "display_name", "email", "password_policy"}


class TestSelfServiceAccount:
    """Issue #75 S3: name/email profile fields and password policy/recovery."""

    def _auth(self, monkeypatch, tmp_path, user, role, password="account-contract-password"):
        monkeypatch.setenv("ADMIN_PASSWORD", password)
        monkeypatch.setenv("ADMIN_USER", "primary-admin")
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        (tmp_path / "installation.json").write_text(
            json.dumps({"installation_id": "test", "completed_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        import hashlib
        import hmac
        from core.security import _hash_secret, _session_token
        monkeypatch.setenv("USERS_JSON", json.dumps({
            user: {"password_hash": _hash_secret(password), "role": role}}))
        token = _session_token(user, role)
        csrf = hmac.new(password.encode(), token.encode(), hashlib.sha256).hexdigest()
        return ({"vertep_session": token, "vertep_csrf": csrf},
                {"x-csrf-token": csrf})

    def test_viewer_can_update_own_profile_fields(self, monkeypatch, tmp_path):
        cookies, headers = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        resp = client.put("/api/session/profile",
                          json={"display_name": "Переглядач Один", "email": "reader@example.com"},
                          cookies=cookies, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Переглядач Один"
        assert resp.json()["email"] == "reader@example.com"
        assert resp.json()["role"] == "viewer"
        # Значення зберігаються і повертаються наступним запитом сесії.
        body = client.get("/api/session", cookies=cookies).json()
        assert body["display_name"] == "Переглядач Один"
        assert body["email"] == "reader@example.com"

    def test_profile_rejects_invalid_fields(self, monkeypatch, tmp_path):
        cookies, headers = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        resp = client.put("/api/session/profile",
                          json={"display_name": "x" * 81, "email": "not-an-email"},
                          cookies=cookies, headers=headers)
        assert resp.status_code == 422
        assert set(resp.json()["fields"]) == {"display_name", "email"}

    def test_profile_requires_a_session(self, monkeypatch, tmp_path):
        self._auth(monkeypatch, tmp_path, "reader", "viewer")
        resp = client.put("/api/session/profile", json={"display_name": "X"})
        assert resp.status_code == 401

    def test_password_change_is_persisted_and_old_password_stops_working(self, monkeypatch, tmp_path):
        cookies, headers = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        resp = client.put("/api/session/password",
                          json={"old_password": "account-contract-password",
                                "new_password": "new-reader-password-12"},
                          cookies=cookies, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Новий пароль приймається сервером, старий більше ні.
        from core.security import _authenticate_user
        assert _authenticate_user("reader", "new-reader-password-12") == "viewer"
        assert _authenticate_user("reader", "account-contract-password") is None

    def test_wrong_current_password_is_recoverable(self, monkeypatch, tmp_path):
        cookies, headers = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        resp = client.put("/api/session/password",
                          json={"old_password": "not-the-password",
                                "new_password": "another-password-12"},
                          cookies=cookies, headers=headers)
        assert resp.status_code == 400
        assert "incorrect" in resp.json()["detail"]
        # Сесія лишається чинною, пароль не змінено: повторна спроба можлива.
        assert client.get("/api/session", cookies=cookies).json()["authenticated"] is True
        retry = client.put("/api/session/password",
                           json={"old_password": "account-contract-password",
                                 "new_password": "recovered-password-12"},
                           cookies=cookies, headers=headers)
        assert retry.status_code == 200

    def test_policy_is_enforced_and_reported(self, monkeypatch, tmp_path):
        cookies, headers = self._auth(monkeypatch, tmp_path, "reader", "viewer")
        same = client.put("/api/session/password",
                          json={"old_password": "account-contract-password",
                                "new_password": "account-contract-password"},
                          cookies=cookies, headers=headers)
        assert same.status_code == 400
        policy = client.get("/api/session", cookies=cookies).json()["password_policy"]
        assert policy["min_length"] == 12
        short = client.put("/api/session/password",
                           json={"old_password": "account-contract-password",
                                 "new_password": "x" * (policy["min_length"] - 1)},
                           cookies=cookies, headers=headers)
        assert short.status_code == 400
        assert str(policy["min_length"]) in short.json()["detail"]


class TestLocalDataRoots:
    """Runtime roots must stay inside the project on a developer machine.

    The appliance paths are Linux absolute paths; on Windows they resolve to the
    drive root (``C:\\data``, ``D:\\tmp``), so the defaults fall back to the
    project instead. Production keeps the appliance paths via the environment.
    """

    PROJECT = Path(__file__).resolve().parent.parent

    def _defaults(self, monkeypatch):
        from core.first_run import default_config_root, default_storage_root, default_update_state_dir
        from core.persistent_data import _data_storage
        for name in ("CONFIG_ROOT", "STORAGE_ROOT", "UPDATE_STATE_DIR", "JOB_ROOT"):
            monkeypatch.delenv(name, raising=False)
        return [default_config_root(), default_storage_root(),
                default_update_state_dir(), _data_storage()]

    def test_unset_roots_resolve_inside_project(self, monkeypatch):
        for path in self._defaults(monkeypatch):
            assert path.is_absolute()
            if os.name == "nt":
                assert str(path.resolve()).lower().startswith(str(self.PROJECT).lower()), path

    def test_environment_overrides_win(self, monkeypatch, tmp_path):
        from core.first_run import default_config_root, default_storage_root, default_update_state_dir
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path / "cfg"))
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "sto"))
        monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "upd"))
        assert default_config_root() == tmp_path / "cfg"
        assert default_storage_root() == tmp_path / "sto"
        assert default_update_state_dir() == tmp_path / "upd"
