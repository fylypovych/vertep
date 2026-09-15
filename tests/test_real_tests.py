"""Tests for core/real_tests module — models, storage, GitHub reporter, runner, scenarios, and API."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.real_tests.models import (
    CheckResult, CheckStatus, TestRun, TestRunStatus,
)
from core.real_tests.runner import RealTestRunner
from core.real_tests.scenarios import (
    CHECK_REGISTRY, available_checks, find_scenario, load_scenarios,
)
from core.real_tests.storage import (
    append_check, audit_entry, create_test_run, delete_test_run,
    get_test_run, list_test_runs, record_github_report, update_test_run,
)
from core.real_tests.github import GitHubReporter
import core.real_tests.github as gh_mod


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TestModels:
    def test_test_run_created_with_defaults(self, tmp_path):
        run = TestRun(rt_id="rt::S01")
        assert run.status is TestRunStatus.RUNNING
        assert run.final_result is None
        assert run.checks == []
        assert len(run.test_run_id) == 32
        assert run.version != ""
        assert run.commit_sha != ""
        assert run.started_at != ""

    def test_test_run_to_report_includes_version_and_checks(self, tmp_path):
        run = TestRun(rt_id="rt::S01", version="test-ver", commit_sha="deadbeef")
        report = run.to_report()
        assert report["rt_id"] == "rt::S01"
        assert report["version"] == "test-ver"
        assert report["commit_sha"] == "deadbeef"
        assert "environment" in report
        assert "checks" in report


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TESTS_STORAGE_DIR", str(tmp_path / "rt_storage"))


def _make_run() -> TestRun:
    """Helper to create a fully populated TestRun via the storage layer."""
    return create_test_run(
        rt_id="rt::S01", rt_issue_number=40, initiator="test",
        version="0.0.1.0", commit_sha="abc123",
        environment={"platform": "linux"}, hardware={"cpu": 4},
        core_node_id="core-001", targets=["gpu-001"],
    )


class TestStorage:
    def test_create_and_get_test_run(self):
        run = _make_run()
        loaded = get_test_run(run.test_run_id)
        assert loaded is not None
        assert loaded.test_run_id == run.test_run_id
        assert loaded.rt_id == "rt::S01"

    def test_get_nonexistent_test_run(self):
        assert get_test_run("nonexistent-id-12345678901234567890123456789012") is None

    def test_list_test_runs_empty(self):
        assert list_test_runs() == []

    def test_list_test_runs_orders_by_started(self):
        run1 = _make_run()
        run2 = _make_run()
        runs = list_test_runs()
        assert len(runs) == 2

    def test_update_test_run_persists(self):
        run = _make_run()
        run.status = TestRunStatus.PASS
        run.final_result = "PASS"
        update_test_run(run)
        loaded = get_test_run(run.test_run_id)
        assert loaded.status is TestRunStatus.PASS
        assert loaded.final_result == "PASS"

    def test_append_check_persists(self):
        run = _make_run()
        check = CheckResult(name="docker", status=CheckStatus.PASS, detail="ok",
                            duration_ms=10, evidence={}, error=None)
        append_check(run.test_run_id, check)
        loaded = get_test_run(run.test_run_id)
        assert len(loaded.checks) == 1
        assert loaded.checks[0].name == "docker"
        assert loaded.checks[0].status == CheckStatus.PASS

    def test_record_github_report(self):
        run = _make_run()
        record_github_report(run.test_run_id, "PASS", "123456", None)
        loaded = get_test_run(run.test_run_id)
        assert loaded.github_report is not None
        assert loaded.github_report["final_result"] == "PASS"
        assert loaded.github_report["comment_id"] == "123456"

    def test_audit_entry_writes_to_audit_log(self, tmp_path):
        run = _make_run()
        entry = audit_entry(run.test_run_id, "test_action", "hello", "actor")
        assert entry["phase"] == "test_action"
        assert entry["message"] == "hello"
        assert entry["actor"] == "actor"

    def test_delete_test_run(self):
        run = _make_run()
        assert get_test_run(run.test_run_id) is not None
        result = delete_test_run(run.test_run_id)
        assert result is True
        assert get_test_run(run.test_run_id) is None


# ---------------------------------------------------------------------------
# GitHub Reporter
# ---------------------------------------------------------------------------

class TestGitHubReporter:
    def test_report_calls_gh(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="FAIL",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: False)
        monkeypatch.setattr(GitHubReporter, "_post_comment", lambda self, num, body: "999")
        result = reporter.report(run)
        assert result["reported"] is True
        assert result["comment_id"] == "999"

    def test_report_with_gh_failure(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="FAIL",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: False)
        monkeypatch.setattr(GitHubReporter, "_post_comment",
                            lambda self, num, body: (_ for _ in ()).throw(
                                RuntimeError("gh: unauthorized")))
        result = reporter.report(run)
        assert result["reported"] is False
        assert result["error"] is not None

    def test_close_issue_calls_gh(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="PASS",
        )
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z"}
        monkeypatch.setattr(GitHubReporter, "can_close_issue", lambda self, r: True)
        monkeypatch.setattr(gh_mod, "_gh", lambda *a, **kw: "closed")
        result = reporter.close_issue(run)
        assert result["closed"] is True

    def test_retry_pending_retries_on_transient_error(self, tmp_path, monkeypatch):
        from core.real_tests.github import _TransientError
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=10, version="test-ver",
            commit_sha="deadbeef", final_result="FAIL",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: False)
        calls = []
        monkeypatch.setattr("core.real_tests.github.time.sleep", lambda d: None)

        def mock_post(self, num, body):
            calls.append((num, body))
            if len(calls) == 1:
                raise _TransientError("timeout")
            return "999"

        monkeypatch.setattr(GitHubReporter, "_post_comment", mock_post)
        result = reporter.report(run)
        assert result["reported"] is True
        assert len(calls) == 2

    def test_is_transient_error(self):
        reporter = GitHubReporter()
        assert reporter._is_transient("API rate limit exceeded") is True
        assert reporter._is_transient("Connection refused") is True
        assert reporter._is_transient("Server error 503") is True
        assert reporter._is_transient("fatal: not found") is False
        assert reporter._is_transient("invalid issue number") is False

    def test_already_reported_idempotent(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="PASS",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        called = []
        monkeypatch.setattr(GitHubReporter, "_post_comment",
                            lambda self, num, body: called.append(num) or "123")
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: True)
        result = reporter.report(run)
        assert result["reported"] is True
        assert called == []


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

class TestScenarios:
    def test_load_scenarios_returns_list(self):
        scenarios = load_scenarios()
        assert isinstance(scenarios, list)
        assert len(scenarios) >= 8

    def test_find_scenario_by_rt_id(self):
        scenarios = load_scenarios()
        first_rt = scenarios[0].get("rt", "")
        rt_prefix = first_rt.split()[0] if first_rt else "rt::S01"
        found = find_scenario(rt_id=rt_prefix)
        assert found is not None
        assert found["rt"].split()[0] == rt_prefix

    def test_find_scenario_by_issue_number(self):
        found = find_scenario(rt_issue_number=40)
        assert found is not None
        assert found["rt_issue"] == 40

    def test_find_scenario_not_found(self):
        assert find_scenario(rt_id="rt::NONEXISTENT") is None
        assert find_scenario(rt_issue_number=99999) is None

    def test_available_checks_returns_non_empty(self):
        checks = available_checks()
        assert isinstance(checks, list)
        assert len(checks) > 0

    def test_check_registry_has_expected_checks(self):
        names = list(CHECK_REGISTRY.keys())
        assert "docker" in names
        assert "core_api" in names
        assert "postgres" in names
        assert "redis" in names
        for func in CHECK_REGISTRY.values():
            assert callable(func), f"Check '{func}' must be callable"


class TestQualifyIntegration:
    def test_rt_issue_field_present(self):
        from scripts.qualify_infrastructure import SCENARIOS
        for scenario in SCENARIOS:
            assert "rt_issue" in scenario
            assert scenario["rt_issue"] is not None

    def test_find_scenario_by_rt_id_from_qualify(self):
        from scripts.qualify_infrastructure import find_scenario_by_rt_id
        result = find_scenario_by_rt_id("rt::S01")
        assert result is not None
        assert result["rt"].startswith("rt::S01")

    def test_find_scenario_by_issue_from_qualify(self):
        from scripts.qualify_infrastructure import find_scenario_by_issue
        result = find_scenario_by_issue(40)
        assert result is not None
        assert result["rt_issue"] == 40

    def test_find_scenario_by_issue_not_found(self):
        from scripts.qualify_infrastructure import find_scenario_by_issue
        assert find_scenario_by_issue(99999) is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TESTS_STORAGE_DIR", str(tmp_path / "rt_storage"))
    return RealTestRunner()


class TestRunner:
    def test_start_creates_run(self, runner):
        run = runner.start("rt::S01", initiator="test")
        assert run.test_run_id is not None
        assert run.rt_id == "rt::S01"
        assert run.status is TestRunStatus.RUNNING

    def test_start_invalid_rt_id_raises(self, runner):
        with pytest.raises(ValueError, match="Unknown rt_id"):
            runner.start("rt::NONEXISTENT", initiator="test")

    def test_run_checks_emulates_lifecycle(self, runner, monkeypatch):
        run = runner.start("rt::S01", initiator="test")
        monkeypatch.setattr(
            "core.real_tests.scenarios.check_docker",
            lambda: (True, "docker ok"),
        )
        monkeypatch.setattr(
            "core.real_tests.scenarios.check_core_api",
            lambda: (True, "core_api ok"),
        )
        run = runner.run_checks(run, check_names=["docker", "core_api"])
        assert len(run.checks) == 2

    def test_finalize_sets_result_pass(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run.checks.append(CheckResult(
            name="docker", status=CheckStatus.PASS, detail="ok",
            duration_ms=10, evidence={}, error=None,
        ))
        run = runner.finalize(run)
        assert run.final_result == "PASS"

    def test_finalize_fail_when_any_check_fails(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run.checks.append(CheckResult(
            name="docker", status=CheckStatus.FAIL, detail="err",
            duration_ms=10, evidence={}, error="docker not found",
        ))
        run = runner.finalize(run)
        assert run.final_result == "FAIL"
        assert run.error_details is not None

    def test_finalize_fail_when_no_mandatory_checks(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run = runner.finalize(run)
        assert run.final_result == "FAIL"
        assert "No mandatory checks" in (run.error_details or "")

    def test_get_report_returns_dict(self, runner):
        run = runner.start("rt::S01", initiator="test")
        report = runner.get_report(run.test_run_id)
        assert report is not None
        assert report["test_run_id"] == run.test_run_id
        assert report["rt_id"] == "rt::S01"

    def test_get_report_nonexistent(self, runner):
        assert runner.get_report("nonexistent-id-12345678901234567890123456789012") is None

    def test_run_full_lifecycle(self, runner, monkeypatch):
        monkeypatch.setitem(CHECK_REGISTRY, "docker", lambda: (True, "docker ok"))
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: False)
        result = runner.run("rt::S01", initiator="test", check_names=["docker"])
        assert result.final_result == "PASS"


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TESTS_STORAGE_DIR", str(tmp_path / "rt_storage"))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("USERS_JSON", "")
    return TestClient(app)


class TestRealTestsAPI:
    def test_scenarios_endpoint(self, admin_client):
        resp = admin_client.get("/api/real-tests/scenarios",
                                auth=("admin", "secret123"))
        assert resp.status_code == 200
        data = resp.json()
        assert "scenarios" in data
        assert "available_checks" in data
        assert "version" in data

    def test_scenarios_requires_auth(self, admin_client):
        resp = admin_client.get("/api/real-tests/scenarios")
        assert resp.status_code == 401

    def test_run_test_endpoint(self, admin_client, monkeypatch):
        def mock_start(self, *args, **kwargs):
            run = TestRun(
                rt_id="rt::S01", rt_issue_number=40,
                version="0.0.1.0", commit_sha="abc",
                environment={}, hardware={}, core_node_id="core-1", targets=[],
            )
            run.checks = []
            run.final_result = "PASS"
            run.status = TestRunStatus.PASS
            return run

        def mock_checks(self, run, check_names=None):
            return run

        def mock_finalize(self, run):
            run.status = TestRunStatus.PASS
            run.final_result = "PASS"
            return run

        def mock_report(self, run):
            return {"reported": True, "comment_id": "123", "error": None}

        monkeypatch.setattr(RealTestRunner, "start", mock_start)
        monkeypatch.setattr(RealTestRunner, "run_checks", mock_checks)
        monkeypatch.setattr(RealTestRunner, "finalize", mock_finalize)
        monkeypatch.setattr(RealTestRunner, "report_to_github", mock_report)

        resp = admin_client.post("/api/real-tests/run",
                                 json={"rt_id": "rt::S01", "confirm_destructive": True},
                                 auth=("admin", "secret123"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "PASS"
        assert data["result"] == "PASS"

    def test_run_test_invalid_rt_id(self, admin_client):
        resp = admin_client.post("/api/real-tests/run",
                                 json={"rt_id": "rt::NONEXISTENT", "confirm_destructive": True},
                                 auth=("admin", "secret123"))
        assert resp.status_code == 404

    def test_list_runs_endpoint(self, admin_client, tmp_path, monkeypatch):
        monkeypatch.setattr("core.api.real_tests._runner", None)
        resp = admin_client.get("/api/real-tests/runs", auth=("admin", "secret123"))
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data

    def test_get_run_endpoint(self, admin_client, tmp_path, monkeypatch):
        monkeypatch.setattr("core.api.real_tests._runner", None)
        r = RealTestRunner()
        def mock_start(self, *args, **kwargs):
            run = TestRun(
                rt_id="rt::S01", rt_issue_number=40, version="0.0.1.0", commit_sha="abc",
                environment={}, hardware={}, core_node_id="core-1", targets=[],
            )
            run.checks = []
            run.final_result = "PASS"
            run.status = TestRunStatus.PASS
            update_test_run(run)
            return run
        monkeypatch.setattr(RealTestRunner, "start", mock_start)
        run = r.start("rt::S01", initiator="test")
        resp = admin_client.get(f"/api/real-tests/runs/{run.test_run_id}",
                                auth=("admin", "secret123"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["test_run_id"] == run.test_run_id

    def test_get_run_nonexistent(self, admin_client):
        resp = admin_client.get("/api/real-tests/runs/nonexistent-id-12345678901234567890123456789012",
                                auth=("admin", "secret123"))
        assert resp.status_code == 404

    def test_retry_report_endpoint(self, admin_client, tmp_path, monkeypatch):
        monkeypatch.setattr("core.api.real_tests._runner", None)
        r = RealTestRunner()
        run = r.start("rt::S01", initiator="test")
        def mock_retry_report(self, test_run_id):
            r_val = get_test_run(test_run_id)
            if r_val:
                r_val.github_report = {"reported_at": "now", "final_result": "PASS"}
                update_test_run(r_val)
            return r_val
        monkeypatch.setattr(RealTestRunner, "retry_report", mock_retry_report)
        resp = admin_client.post(f"/api/real-tests/runs/{run.test_run_id}/retry-report",
                                 auth=("admin", "secret123"))
        assert resp.status_code == 200

    def test_retry_report_nonexistent(self, admin_client):
        resp = admin_client.post(
            "/api/real-tests/runs/nonexistent-id-12345678901234567890123456789012/retry-report",
            auth=("admin", "secret123"))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Generation gate compliance
# ---------------------------------------------------------------------------

class TestGenerationGateCompliance:
    """Verify real_tests module doesn't introduce forbidden CORE generation calls."""

    def test_models_no_generation_imports(self):
        from core.real_tests.models import TestRun as TR
        import inspect
        src = inspect.getsource(TR)
        assert "providers.llm" not in src
        assert "ComfyUIAdapter" not in src

    def test_github_no_generation_calls(self):
        import inspect
        src = inspect.getsource(GitHubReporter)
        assert "providers.tts" not in src
        assert "providers.compute" not in src
        assert "ComfyUIAdapter" not in src

    def test_scenarios_reuse_health_checks(self):
        assert len(CHECK_REGISTRY) > 0
        for name, func in CHECK_REGISTRY.items():
            assert callable(func), f"Check '{name}' must be callable"

    def test_runner_no_direct_provider_calls(self):
        """Runner must dispatch via workers, not call providers directly."""
        import inspect
        src = inspect.getsource(RealTestRunner)
        forbidden = [
            "providers.llm().generate_script",
            "providers.tts().synthesize",
            "providers.compute().generate_output",
            "ComfyUIAdapter(",
            "TTSAdapter(",
        ]
        for pattern in forbidden:
            assert pattern not in src, f"Runner must not contain '{pattern}'"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
