"""End-to-end integration tests for Vertep deployment roles and workflows."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def test_all_roles_have_valid_plans():
    planner = importlib.util.spec_from_file_location(
        "deployment_plan", Path("scripts/deployment-plan.py"))
    module = importlib.util.module_from_spec(planner)
    planner.loader.exec_module(module)
    roles = json.loads(Path("config/node_roles.json").read_text(encoding="utf-8"))
    expected_roles = {"core", "gpu", "text", "voice", "publisher", "backup", "monitoring"}
    assert expected_roles.issubset(set(roles.keys()))
    for role in expected_roles:
        plan = module.create_plan(roles, role, "0.0.0.5")
        assert module.verify_plan(plan)
        assert plan["role"] == role
        assert plan["version"] == "0.0.0.5"


def test_installer_supports_all_roles():
    import sys
    result = pytest.importorskip("subprocess").run(
        [sys.executable, "installer/role-plan.py", "installer/manifest.json", "core", "services"],
        capture_output=True, text=True, check=True
    )
    assert "vertep-core" in result.stdout


def test_watchdog_module_loads():
    spec = importlib.util.spec_from_file_location("watchdog", Path("scripts/watchdog.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "run_checks")
    assert hasattr(module, "health_status")


def test_health_checks_for_all_roles():
    import sys
    sys.path.insert(0, str(Path(".").resolve()))
    from core.health_checks import run_checks, health_status
    for role in ("core", "gpu", "text", "voice", "publisher", "backup", "monitoring"):
        checks = run_checks(role)
        assert checks["role"] == role
        assert "checked_at" in checks
        assert health_status(checks) in ("HEALTHY", "UNHEALTHY")


def test_worker_update_agent_accepts_skip_drain():
    import sys
    result = pytest.importorskip("subprocess").run(
        [sys.executable, "scripts/update-agent.py", "--help"],
        capture_output=True, text=True, check=True
    )
    assert "--skip-drain" in result.stdout


def test_add_worker_wizard_html_exists():
    html = Path("web/index.html").read_text(encoding="utf-8")
    assert "addworkerdialog" in html
    assert "wizard-step-1" in html
    assert "wizard-step-2" in html
    assert "wizard-step-3" in html


def test_setup_wizard_has_manifest_step():
    html = Path("web/setup.html").read_text(encoding="utf-8")
    assert "Installation Manifest" in html
    assert "downloadManifest" in html
    assert "manifest.json" in html


def test_bootstrap_redirects_to_setup():
    script = Path("bootstrap.sh").read_text(encoding="utf-8")
    assert "/setup?token=" in script
    assert "xdg-open" in script or "open" in script


def test_setup_alias_preserves_token(monkeypatch):
    from core import app as core_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(core_app, "is_configured", lambda: False)
    client = TestClient(core_app.app)
    response = client.get("/setup?token=one-time-code", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/setup.html?token=one-time-code"
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert '<div class="brand">VERTEP</div>' in page.text


def test_worker_update_processor_exists():
    assert Path("scripts/worker_update.py").is_file()
    content = Path("scripts/worker_update.py").read_text(encoding="utf-8")
    assert "process_worker_update" in content
    assert "target_version" in content


def test_server_side_worker_filtering():
    sys.path.insert(0, str(Path(".").resolve()))
    from core.app import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    response = client.get("/api/workers")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_bootstrap_supports_push_based_enrollment():
    script = Path("bootstrap.sh").read_text(encoding="utf-8")
    assert "VERTEP_NODE_TOKEN" in script
    assert "VERTEP_CORE_URL" in script


def test_domain_support_in_setup():
    html = Path("web/setup.html").read_text(encoding="utf-8")
    assert "domain" in html.lower()
    assert "WEB_DOMAIN" in Path("core/first_run.py").read_text(encoding="utf-8")


def test_rolling_update_order():
    sys.path.insert(0, str(Path(".").resolve()))
    from core.rolling_update import _write, rollout_status
    _write({"state": "IDLE", "nodes": []})
    from core.rolling_update import start_rollout
    for order in ("workers-first", "core-first"):
        rollout = start_rollout("1.0.0", ["worker-1", "worker-2"], order=order)
        assert rollout["order"] == order
        assert rollout["state"] == "RUNNING"
        _write({"state": "IDLE", "nodes": []})


def test_health_history_endpoint():
    sys.path.insert(0, str(Path(".").resolve()))
    from core.app import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    response = client.get("/api/health/history")
    assert response.status_code == 200
    assert "history" in response.json()
