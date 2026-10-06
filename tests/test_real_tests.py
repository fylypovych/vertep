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
from core.real_tests.github import GitHubReporter, _TransientError
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
    def test_report_calls_api(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="FAIL",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: False)
        monkeypatch.setattr(gh_mod, "_post_comment", lambda num, body: "999")
        result = reporter.report(run)
        assert result["reported"] is True
        assert result["comment_id"] == "999"

    def test_report_with_api_failure(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="FAIL",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: False)
        monkeypatch.setattr(gh_mod, "_post_comment",
                            lambda num, body: (_ for _ in ()).throw(
                                RuntimeError("gh: unauthorized")))
        result = reporter.report(run)
        assert result["reported"] is False
        assert result["error"] is not None

    def test_close_issue_calls_api(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="PASS",
        )
        run.checks = [
            CheckResult(name="docker", status=CheckStatus.PASS, detail="ok"),
        ]
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z",
                             "version": "test-ver", "commit_sha": "deadbeef"}
        monkeypatch.setenv("GITHUB_REPOSITORY", "test-repo")
        monkeypatch.setattr(GitHubReporter, "can_close_issue", lambda self, r: True)
        monkeypatch.setattr(gh_mod, "_close_issue", lambda num: None)
        result = reporter.close_issue(run)
        assert result["closed"] is True

    def test_retry_pending_retries_on_transient_error(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=10, version="test-ver",
            commit_sha="deadbeef", final_result="FAIL",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(GitHubReporter, "_already_reported", lambda self, tid, num: False)
        calls = []
        monkeypatch.setattr("core.real_tests.github.time.sleep", lambda d: None)

        def mock_post(num, body):
            calls.append((num, body))
            if len(calls) == 1:
                raise _TransientError("timeout")
            return "999"

        monkeypatch.setattr(gh_mod, "_post_comment", mock_post)
        result = reporter.report(run)
        assert result["reported"] is True
        assert len(calls) == 2

    def test_already_reported_idempotent(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="deadbeef", final_result="PASS",
        )
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        called = []
        monkeypatch.setattr(gh_mod, "_post_comment",
                            lambda num, body: called.append(num) or "123")
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
        found = find_scenario(rt_issue_number=35)
        assert found is not None
        assert found["rt_issue"] == 35

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


class TestScenarioSpecificChecks:
    """Issue i.0.0.0.89 — scenarios must not borrow generic health probes."""

    def test_every_scenario_has_checks(self):
        for scenario_id in ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
            from core.real_tests.scenarios import _scenario_checks
            checks = _scenario_checks(scenario_id)
            assert checks, f"{scenario_id} must declare scenario-specific checks"

    def test_no_scenario_falls_back_to_generic_probes(self):
        from core.real_tests.scenarios import _scenario_checks
        generic = {"docker", "core_api", "health_core", "monitoring",
                   "ollama_probe", "postgres_tcp", "redis_tcp", "tts",
                   "publisher", "backup"}
        for scenario_id in ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
            checks = set(_scenario_checks(scenario_id))
            assert not checks & generic, (
                f"{scenario_id} borrows generic checks: {checks & generic}"
            )

    def test_unknown_scenario_id_has_no_checks(self):
        from core.real_tests.scenarios import _scenario_checks
        assert _scenario_checks("S99") == []

    def test_run_rt_check_names_unknown_is_empty(self):
        from core.real_tests.runner import run_rt_check_names
        assert run_rt_check_names("rt::NONEXISTENT") == []

    def test_run_rt_check_names_returns_scenario_checks(self):
        from core.real_tests.runner import run_rt_check_names
        assert run_rt_check_names("rt::S04") == ["backup_roundtrip"]

    def test_scenario_checks_are_registered(self):
        from core.real_tests.scenarios import _scenario_checks
        for scenario_id in ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
            for name in _scenario_checks(scenario_id):
                assert name in CHECK_REGISTRY, f"{scenario_id}: '{name}' not registered"

    def test_success_policy_is_pass_only(self):
        from core.real_tests.scenarios import get_scenario_success_policy
        for scenario_id in ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
            assert get_scenario_success_policy(scenario_id) == {CheckStatus.PASS}

    def test_success_policy_blocks_close_gate_statuses(self):
        from core.real_tests.scenarios import get_scenario_success_policy
        blocked = {CheckStatus.WARNING, CheckStatus.SKIPPED,
                   CheckStatus.FAIL, CheckStatus.NOT_CONFIGURED}
        for scenario_id in ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
            policy = get_scenario_success_policy(scenario_id)
            assert not policy & blocked
            assert policy <= {CheckStatus.PASS}


class TestProcedureChecks:
    """Procedure checks report not-executed instead of a borrowed PASS."""

    PROCEDURE_NAMES = ["bootstrap_first_run", "backup_roundtrip",
                       "migration_artifacts", "update_interrupt",
                       "release_signature", "publisher_receipt"]

    def test_registered_and_return_none(self):
        for name in self.PROCEDURE_NAMES:
            fn = CHECK_REGISTRY[name]
            passed, detail = fn()
            assert passed is None, f"{name} must be not-executed, got {passed}"
            assert "procedure not executed" in detail

    def test_runner_records_skipped_for_none(self, runner):
        run = runner.start("rt::S01", initiator="test")
        executed = runner.run_checks(run, check_names=[])
        assert len(executed.checks) == 1
        check = executed.checks[0]
        assert check.name == "bootstrap_first_run"
        assert check.status is CheckStatus.SKIPPED

    def test_finalize_procedure_when_only_skipped_failures(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run.checks.append(CheckResult(name="bootstrap_first_run",
                                      status=CheckStatus.SKIPPED,
                                      detail="procedure not executed"))
        run = runner.finalize(run)
        assert run.final_result == "PROCEDURE"
        assert run.final_result != "PASS"

    def test_finalize_fail_when_warning_present(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run.checks.append(CheckResult(name="a", status=CheckStatus.PASS, detail="ok"))
        run.checks.append(CheckResult(name="b", status=CheckStatus.WARNING,
                                      detail="warn"))
        run = runner.finalize(run)
        assert run.final_result == "FAIL"

    def test_finalize_fail_when_mixed_failure_and_skipped(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run.checks.append(CheckResult(name="a", status=CheckStatus.FAIL,
                                      detail="broken"))
        run.checks.append(CheckResult(name="b", status=CheckStatus.SKIPPED,
                                      detail="procedure not executed"))
        run = runner.finalize(run)
        assert run.final_result == "FAIL"

    def test_finalize_pass_requires_every_mandatory_pass(self, runner):
        run = runner.start("rt::S01", initiator="test")
        run.checks.append(CheckResult(name="a", status=CheckStatus.PASS, detail="ok"))
        run.checks.append(CheckResult(name="b", status=CheckStatus.PASS, detail="ok"))
        run = runner.finalize(run)
        assert run.final_result == "PASS"


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
        result = find_scenario_by_issue(35)
        assert result is not None
        assert result["rt_issue"] == 35

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
        monkeypatch.setattr(
            "core.real_tests.scenarios._check_health_core",
            lambda: (True, "health ok"),
        )
        run = runner.run_checks(run, check_names=["docker", "core_api"])
        assert len(run.checks) == 3

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
        monkeypatch.setitem(CHECK_REGISTRY, "bootstrap_first_run",
                            lambda: (None, "procedure not executed in-process"))
        monkeypatch.setattr(gh_mod, "_is_configured", lambda: False)
        result = runner.run("rt::S01", initiator="test", check_names=["docker"])
        assert result.final_result == "PROCEDURE"

    def test_run_full_lifecycle_pass_when_all_checks_pass(self, runner, monkeypatch):
        monkeypatch.setitem(CHECK_REGISTRY, "docker", lambda: (True, "docker ok"))
        monkeypatch.setitem(CHECK_REGISTRY, "bootstrap_first_run", lambda: (True, "ok"))
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


class TestSecretRedaction:
    """Negative tests for secret redaction patterns."""

    def test_redact_key_value_pairs(self):
        from core.real_tests.runner import _redact_secrets
        cases = [
            ("api_key=sk-1234567890abcdef", "api_key = ***REDACTED***"),
            ("token: ghp_abcdefghij1234567890", "token = ***REDACTED***"),
            ("password = mysecretpassword123", "password = ***REDACTED***"),
            ("AWS_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE", "AWS_ACCESS_KEY = ***REDACTED***"),
            ("private_key:MIIEvgIBADANBg", "private_key = ***REDACTED***"),
            ("credentials: userpass12345678", "credentials = ***REDACTED***"),
        ]
        for text, expected in cases:
            result = _redact_secrets(text)
            assert "***REDACTED***" in result, f"Failed to redact: {text}"
            assert "sk-1234567890abcdef" not in result
            assert "ghp_abcdefghij1234567890" not in result
            assert "mysecretpassword123" not in result

    def test_redact_json_values(self):
        from core.real_tests.runner import _redact_secrets
        text = '{"api_key": "sk-proj-1234567890abcdef", "token": "ghp_abcdefghij1234567890"}'
        result = _redact_secrets(text)
        assert "sk-proj-1234567890abcdef" not in result
        assert "ghp_abcdefghij1234567890" not in result
        assert "***REDACTED***" in result

    def test_redact_extended_patterns(self):
        from core.real_tests.runner import _redact_secrets
        cases = [
            "credentials: userpass12345678",
            "access_key=AKIAIOSFODNN7EXAMPLE",
            "client_secret=abcdefghijklmnopqrstuvwxyz123456",
            "jwt_secret: mysupersecretjwtkey123456",
            "encryption_key=abcdef1234567890abcdef",
            "internal_api_key: iv1234567890abcdef",
        ]
        for text in cases:
            result = _redact_secrets(text)
            assert "***REDACTED***" in result, f"Failed to redact: {text}"

    def test_redact_short_values_not_redacted(self):
        from core.real_tests.runner import _redact_secrets
        text = "token=short"
        result = _redact_secrets(text)
        assert "short" in result
        assert "***REDACTED***" not in result

    def test_redact_clean_text_unchanged(self):
        from core.real_tests.runner import _redact_secrets
        text = "All checks passed. Docker is running. PostgreSQL connected."
        result = _redact_secrets(text)
        assert result == text

    def test_redact_mixed_content(self):
        from core.real_tests.runner import _redact_secrets
        text = (
            "Check docker: OK\n"
            "api_key=sk-1234567890abcdef12345678\n"
            "Check postgres: OK\n"
            "password=SuperSecretPassword123456"
        )
        result = _redact_secrets(text)
        assert "docker: OK" in result
        assert "postgres: OK" in result
        assert "sk-1234567890abcdef12345678" not in result
        assert "SuperSecretPassword123456" not in result

    def test_redact_already_redacted(self):
        from core.real_tests.runner import _redact_secrets
        text = "token = ***REDACTED***"
        result = _redact_secrets(text)
        assert result.count("***REDACTED***") == 1

    def test_secret_pattern_covers_all_key_names(self):
        """Verify the regex covers all expected secret key names."""
        import re
        from core.real_tests.runner import _SECRET_PATTERN
        # Key names that match the regex directly
        direct_keys = [
            "api_key", "apikey", "token", "secret", "password", "passwd", "pwd",
            "private_key", "access_key", "client_secret", "session_secret",
            "jwt_secret", "encryption_key", "internal_api_key", "credentials",
        ]
        for name in direct_keys:
            text = f"{name}=a1b2c3d4e5f6g7h8"
            assert _SECRET_PATTERN.search(text), f"Pattern should match key '{name}'"
        # aws_ and ghp_ are prefix-based; they match when the VALUE starts with the prefix
        assert _SECRET_PATTERN.search("token=ghp_abcdefghij1234567890")
        assert _SECRET_PATTERN.search("token=AKIAIOSFODNN7EXAMPLE")


# ---------------------------------------------------------------------------
# Issue #95 — adapter, deployment identity, auto-close
# ---------------------------------------------------------------------------

class TestIssue95:
    """Regression tests for i.0.0.0.95 (zero-arg core_api, honest identity,
    strict acceptance for auto-close)."""

    def test_core_api_registry_entry_is_zero_argument_callable(self, monkeypatch):
        monkeypatch.delenv("CORE_ADDRESS", raising=False)
        result = CHECK_REGISTRY["core_api"]()
        assert isinstance(result, tuple) and len(result) == 2
        status, detail = result
        assert status is None, "unconfigured CORE must be not-applicable, not ok"
        assert "not-applicable" in detail

    def test_core_api_registry_entry_uses_core_address(self, monkeypatch):
        called = {}

        def fake_check(core_url: str):
            called["url"] = core_url
            return True, "ok"

        monkeypatch.setenv("CORE_ADDRESS", "http://core:8080")
        monkeypatch.setattr("core.real_tests.scenarios.check_core_api", fake_check)
        assert CHECK_REGISTRY["core_api"]() == (True, "ok")
        assert called["url"] == "http://core:8080"

    def test_git_commit_has_no_version_or_unknown_fallback(self, monkeypatch):
        from core.real_tests import runner as runner_mod
        monkeypatch.delenv("GITHUB_SHA", raising=False)

        def boom(*args, **kwargs):
            raise OSError("no git")

        monkeypatch.setattr(runner_mod.subprocess, "run", boom)
        assert runner_mod._git_commit() == "", "identity must fail closed, not fall back"

    def test_run_without_identity_cannot_close_issue(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="unknown", final_result="PASS",
        )
        run.checks = [CheckResult(name="docker", status=CheckStatus.PASS)]
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z",
                             "version": "test-ver", "commit_sha": "unknown"}
        assert reporter.can_close_issue(run) is False

    def test_missing_reported_identity_cannot_close_issue(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        sha = "a" * 40
        monkeypatch.setattr(gh_mod, "_deployment_sha", lambda: sha)
        monkeypatch.setattr(gh_mod, "_deployment_version", lambda: "test-ver")
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha=sha, final_result="PASS",
        )
        run.checks = [CheckResult(name="docker", status=CheckStatus.PASS)]
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z"}
        assert reporter.can_close_issue(run) is False

    def test_partial_mandatory_set_cannot_close_issue(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        sha = "b" * 40
        monkeypatch.setattr(gh_mod, "_deployment_sha", lambda: sha)
        monkeypatch.setattr(gh_mod, "_deployment_version", lambda: "test-ver")
        monkeypatch.setattr(gh_mod, "_expected_mandatory_names",
                            lambda rt_id: {"docker", "core_api"})
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha=sha, final_result="PASS",
        )
        run.checks = [CheckResult(name="docker", status=CheckStatus.PASS)]
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z",
                             "version": "test-ver", "commit_sha": sha}
        assert reporter.can_close_issue(run) is False

    def test_mismatched_expected_identity_cannot_close_issue(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        sha = "c" * 40
        monkeypatch.setattr(gh_mod, "_deployment_sha", lambda: sha)
        monkeypatch.setattr(gh_mod, "_deployment_version", lambda: "test-ver")
        monkeypatch.setattr(gh_mod, "_expected_mandatory_names", lambda rt_id: set())
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha="d" * 40, final_result="PASS",
        )
        run.checks = [CheckResult(name="docker", status=CheckStatus.PASS)]
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z",
                             "version": "test-ver", "commit_sha": "d" * 40}
        assert reporter.can_close_issue(run) is False

    def test_full_identity_and_coverage_can_close_issue(self, tmp_path, monkeypatch):
        reporter = GitHubReporter()
        sha = "e" * 40
        monkeypatch.setattr(gh_mod, "_deployment_sha", lambda: sha)
        monkeypatch.setattr(gh_mod, "_deployment_version", lambda: "test-ver")
        monkeypatch.setattr(gh_mod, "_expected_mandatory_names",
                            lambda rt_id: {"docker", "core_api"})
        run = TestRun(
            rt_id="rt::S01", rt_issue_number=40, version="test-ver",
            commit_sha=sha, final_result="PASS",
        )
        run.checks = [
            CheckResult(name="docker", status=CheckStatus.PASS),
            CheckResult(name="core_api", status=CheckStatus.PASS),
        ]
        run.github_report = {"reported_at": "2026-01-01T00:00:00Z",
                             "version": "test-ver", "commit_sha": sha}
        assert reporter.can_close_issue(run) is True


# ---------------------------------------------------------------------------
# Issue #96 — GitHub reporting pagination & exactly-once
# ---------------------------------------------------------------------------

class TestIssue96:
    """Regression tests for i.0.0.0.96 (pagination, exactly-once reporting)."""

    def test_get_comments_paginates_beyond_page_1(self, monkeypatch):
        """_get_comments must follow pagination until the last page."""
        from core.real_tests import github as gh_mod

        page1 = [{"id": i, "body": f"comment {i}"} for i in range(100)]
        page2 = [{"id": 100, "body": "marker on page 2"}]
        calls = []

        def fake_api(method, url, body=None, timeout=30):
            calls.append(url)
            # Match on the page= query parameter, not substrings of per_page=
            if "page=2" in url:
                return page2
            return page1

        monkeypatch.setattr(gh_mod, "_api_request", fake_api)
        monkeypatch.setattr(gh_mod, "_repo", lambda: "fylypovych/vertep")
        comments = gh_mod._get_comments(40)
        assert len(comments) == 101
        assert comments[-1]["body"] == "marker on page 2"
        assert len(calls) >= 2

    def test_get_comments_stops_on_short_page(self, monkeypatch):
        """A page with <100 comments is the last page."""
        from core.real_tests import github as gh_mod

        page1 = [{"id": i, "body": f"c{i}"} for i in range(50)]
        calls = []

        def fake_api(method, url, body=None, timeout=30):
            calls.append(url)
            return page1

        monkeypatch.setattr(gh_mod, "_api_request", fake_api)
        monkeypatch.setattr(gh_mod, "_repo", lambda: "fylypovych/vertep")
        comments = gh_mod._get_comments(40)
        assert len(comments) == 50
        assert len(calls) == 1

    def test_already_reported_finds_marker_on_second_page(self, monkeypatch):
        """_already_reported must scan all pages, not just the first."""
        from core.real_tests import github as gh_mod

        test_run_id = "a" * 32
        page1 = [{"id": i, "body": f"other {i}"} for i in range(100)]
        page2 = [{"id": 100, "body": f"REAL-TEST-RUN: {test_run_id} {test_run_id}"}]
        calls = []

        def fake_api(method, url, body=None, timeout=30):
            calls.append(url)
            if "page=2" in url:
                return page2
            return page1

        monkeypatch.setattr(gh_mod, "_api_request", fake_api)
        monkeypatch.setattr(gh_mod, "_repo", lambda: "fylypovych/vertep")
        reporter = gh_mod.GitHubReporter()
        assert reporter._already_reported(test_run_id, 40) is True
        assert len(calls) >= 2

    def test_concurrent_post_is_idempotent(self, monkeypatch):
        """Two concurrent report() calls for the same run must not double-post."""
        from core.real_tests import github as gh_mod

        monkeypatch.setattr(gh_mod, "_is_configured", lambda: True)
        monkeypatch.setattr(gh_mod, "_deployment_sha", lambda: "a" * 40)
        monkeypatch.setattr(gh_mod, "_deployment_version", lambda: "test-ver")
        monkeypatch.setattr(gh_mod, "_repo", lambda: "fylypovych/vertep")
        monkeypatch.setattr(gh_mod, "audit_entry", lambda *a, **kw: None)

        post_count = [0]

        def fake_post(issue_number, body):
            post_count[0] += 1
            return f"comment-{post_count[0]}"

        monkeypatch.setattr(gh_mod, "_post_comment", fake_post)
        monkeypatch.setattr(gh_mod, "record_github_report",
                            lambda *a, **kw: None)

        run = TestRun(rt_id="rt::S01", rt_issue_number=40, version="test-ver",
                      commit_sha="a" * 40, final_result="PASS")
        run.checks = [CheckResult(name="docker", status=CheckStatus.PASS)]

        reporter = gh_mod.GitHubReporter()
        # First report: no existing marker → post
        monkeypatch.setattr(gh_mod, "_get_comments", lambda n: [])
        r1 = reporter.report(run)
        assert r1["reported"] is True
        assert post_count[0] == 1

        # Second concurrent report: marker already present → skip
        monkeypatch.setattr(gh_mod, "_get_comments",
                            lambda n: [{"body": f"REAL-TEST-RUN: {run.test_run_id} {run.test_run_id}"}])
        r2 = reporter.report(run)
        assert r2["reported"] is True
        assert post_count[0] == 1, "exactly-once: second concurrent post must be skipped"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
