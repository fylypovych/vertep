"""#456: MoneyPrinterTurbo shipped with Vertep, ShortGPT removed, Native readiness scope.

Subprocesses, Docker and the runtime are simulated: this proves the contracts between
the switch, the deployment executor and the Worker, not a live render.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import engine_config
from core.api import settings, system
from core.deployment_plan import create_plan
from core.node_registry import node_roles

ROOT = Path(__file__).resolve().parents[1]


def test_engine_runtime_brings_the_worker_that_executes_its_assembly():
    plan = create_plan(node_roles(), "core", "0.0.4.0", ["voice"], ["moneyprinter"])
    assert plan["engine_runtimes"] == ["moneyprinter"]
    assert {"moneyprinter", "worker"} <= set(plan["services"])
    # An external engine is never executed in CORE (Issue #122 P5): a Worker assembles.
    assert "video_assembly" in plan["capabilities"]
    assert create_plan(node_roles(), "core", "0.0.4.0", ["voice"])["sha256"] != plan["sha256"]
    with pytest.raises(ValueError):
        create_plan(node_roles(), "core", "0.0.4.0", [], ["shortgpt"])
    with pytest.raises(ValueError):
        create_plan(node_roles(), "gpu", "0.0.4.0", [], ["moneyprinter"])


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(system, "update_status", lambda: {})
    (tmp_path / "installation.json").write_text(json.dumps({"completed_at": "now", "node_role": "core",
                                                             "ai_backend": {"type": "skip"}}))
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    app = FastAPI()
    from core.error_codes import install_error_codes
    install_error_codes(app)
    app.include_router(system.router)
    app.include_router(settings.router)
    return TestClient(app), tmp_path


def _save_plan(root, extras, runtimes=None):
    plan = create_plan(node_roles(), "core", system.application_version(), extras, runtimes)
    (root / "deployment-plan.json").write_text(json.dumps(plan))
    return plan


def test_local_switch_first_deploys_the_runtime_through_the_executor(api):
    client, root = api
    _save_plan(root, ["voice"])
    response = client.post("/api/settings/providers/video_engine", json={"backend": "money-printer", "runtime": "local"})
    assert response.status_code == 202 and response.json()["state"] == "RUNTIME_DEPLOYING"
    request = json.loads((root / "deployment-request.json").read_text())
    # No URL or token from the operator; the roles already applied stay.
    assert request["engine_runtimes"] == ["moneyprinter"] and request["additional_roles"] == ["voice"]
    assert "endpoint" not in request and "token" not in json.dumps(request).lower()
    assert response.json()["local_runtime"]["deploying"] is True
    # A repeated click while it deploys does not queue a conflicting request.
    again = client.post("/api/settings/providers/video_engine", json={"backend": "money-printer", "runtime": "local"})
    assert again.status_code == 202


def test_local_switch_uses_the_internal_address_once_deployed(api, monkeypatch):
    client, root = api
    _save_plan(root, [], ["moneyprinter"])
    seen = {}

    def switch(slot, backend, actor="web", endpoint=None):
        seen.update(slot=slot, backend=backend, endpoint=endpoint)
        return {"slot": slot, "backend": backend, "changed": True}

    monkeypatch.setattr(settings, "switch_provider", switch)
    monkeypatch.setattr("core.provider_switch.provider_status_matrix", lambda: {})
    response = client.post("/api/settings/providers/video_engine", json={"backend": "money-printer", "runtime": "local"})
    assert response.status_code == 200
    assert seen == {"slot": "video_engine", "backend": "money-printer", "endpoint": engine_config.LOCAL_MONEY_PRINTER_URL}


def test_local_runtime_refused_where_its_image_does_not_exist(api, monkeypatch):
    client, root = api
    _save_plan(root, [])
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    response = client.post("/api/settings/providers/video_engine", json={"backend": "money-printer", "runtime": "local"})
    assert response.status_code == 409
    assert response.json()["code"] == "local_engine_runtime_is_not_available_on_this_server"
    assert not (root / "deployment-request.json").exists()
    assert client.post("/api/settings/providers/video_engine",
                       json={"backend": "native", "runtime": "local"}).status_code == 422


def test_role_change_keeps_the_engine_runtime(api):
    client, root = api
    _save_plan(root, [], ["moneyprinter"])
    assert client.post("/api/system/roles", json={"roles": ["text"]}).json()["state"] == "QUEUED"
    request = json.loads((root / "deployment-request.json").read_text())
    assert request["engine_runtimes"] == ["moneyprinter"] and request["additional_roles"] == ["text"]


def test_executor_gives_the_local_worker_the_assembly_of_the_runtime(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deployment_456", ROOT / "scripts/apply-deployment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("UPDATE_DATABASE_URL", raising=False)
    config = tmp_path / "config"
    config.mkdir()
    catalog = node_roles()
    plan = create_plan(catalog, "core", "0.0.4.0", [], ["moneyprinter"])
    (tmp_path / ".env").write_text("VERTEP_VERSION=0.0.4.0\n")
    (config / "node_roles.json").write_text(json.dumps(catalog))
    (config / "deployment-request.json").write_text(json.dumps({
        "role": "core", "version": "0.0.4.0", "additional_roles": [], "engine_runtimes": ["moneyprinter"],
        "plan_sha256": plan["sha256"], "request_id": "op", "ai_backend": "skip"}))
    monkeypatch.setattr(module, "ensure_persistent_user_data_legacy_migration", lambda: None)
    monkeypatch.setattr(module, "migrate_staged_legacy_roots", lambda path: None)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "ps" in command:
            rows = [{"Service": service, "State": "exited" if service == "migrate" else "running",
                     "ExitCode": 0, "Health": "healthy"} for service in plan["services"]]
            return SimpleNamespace(stdout="\n".join(json.dumps(row) for row in rows), returncode=0)
        return SimpleNamespace(stdout="", returncode=1)

    assert module.apply(tmp_path, runner)["state"] == "SUCCEEDED"
    up = next(command for command in calls if "up" in command)
    assert {"moneyprinter", "worker"} <= set(up)
    env = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines() if "=" in line)
    assert env["NODE_ENGINE_RUNTIMES"] == "moneyprinter"
    assert "video_assembly" in env["NODE_CAPABILITIES"].split(",")
    assert "assembly" in env["SUPPORTED_TASKS"].split(",")
    assert json.loads((config / "deployment-plan.json").read_text())["engine_runtimes"] == ["moneyprinter"]


def test_core_worker_self_test_proves_the_local_runtime(monkeypatch):
    from worker import service
    monkeypatch.setenv("NODE_ADDITIONAL_ROLES", "")
    monkeypatch.setenv("NODE_ENGINE_RUNTIMES", "moneyprinter")
    asked = []
    monkeypatch.setattr(service.httpx, "get", lambda url, timeout: asked.append(url) or SimpleNamespace(
        status_code=200, content=b'{"status":"ready"}', raise_for_status=lambda: None))
    result = service.role_self_test("core", {})
    assert result["status"] == "PASSED" and asked == ["http://moneyprinter:8098/health"]
    assert result["evidence"]["moneyprinter"]["http_status"] == 200

    def down(url, timeout):
        raise service.httpx.ConnectError("refused")
    monkeypatch.setattr(service.httpx, "get", down)
    assert service.role_self_test("core", {})["status"] == "FAILED"


def test_native_ready_means_ffmpeg_in_core_and_nothing_more(monkeypatch):
    from adapters.providers import NativeVertepEngine
    monkeypatch.setattr(engine_config, "native_assembly_available", lambda: False)
    report = engine_config.effective_engine_config(engine=NativeVertepEngine())
    assert report["ready"] is False and report["reason"] == "ffmpeg_missing"
    # The Worker's activation report for Native does not depend on its own FFmpeg.
    assert engine_config.verify_effective_engine(engine=NativeVertepEngine())["ready"] is True
    monkeypatch.setattr(engine_config, "native_assembly_available", lambda: True)
    assert engine_config.effective_engine_config(engine=NativeVertepEngine())["ready"] is True


def test_return_to_native_is_never_trapped_by_its_readiness(monkeypatch, tmp_path):
    """Native is the fallback: missing FFmpeg is reported, not a refusal to switch back."""
    from core import provider_switch
    monkeypatch.setattr(engine_config, "native_assembly_available", lambda: False)
    monkeypatch.setattr(engine_config, "effective_engine_config", lambda probe=False: {
        "effective": "native", "agree": True, "ready": False, "reason": "ffmpeg_missing"})
    assert provider_switch._verify_effective_engine("video_engine", "native")["effective_engine"]["reason"] == "ffmpeg_missing"
    monkeypatch.setattr(engine_config, "effective_engine_config", lambda probe=False: {
        "effective": "money-printer", "agree": True, "ready": False, "reason": "upstream_unreachable"})
    with pytest.raises(provider_switch.ProviderSwitchError):
        provider_switch._verify_effective_engine("video_engine", "money-printer")


def test_shortgpt_is_gone_from_code_configuration_and_ui():
    sources = [*ROOT.glob("adapters/**/*.py"), *ROOT.glob("core/**/*.py"), *ROOT.glob("worker/**/*.py"),
               *ROOT.glob("web-v2/src/**/*.ts"), ROOT / ".env.example", ROOT / "docs/providers.md"]
    offenders = [str(path.relative_to(ROOT)) for path in sources
                 if path.is_file() and "shortgpt" in path.read_text(encoding="utf-8", errors="ignore").lower()
                 and path.name != "__init__.py"]
    assert offenders == []
