"""Application module containers on a node whose Worker has no Docker access.

The Worker asks the host executor (vertep-deployment.service) through
config/application-modules/request.json; the host verifies each package again,
starts the hardened container on the module's own network, joins only CORE
(alias ``core``) and the Worker to it and reports the state back.
"""

import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace


def executor():
    path = Path(__file__).parents[1] / "scripts" / "apply-deployment.py"
    spec = importlib.util.spec_from_file_location("apply_deployment_modules", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def manifest(module_id="brands", version="0.0.4.16"):
    return SimpleNamespace(kind="application", module_id=module_id, version=version,
                           container={"image": "ghcr.io/x/m@sha256:" + "0" * 64, "port": 8080}, resource_requirements={},
                           to_dict=lambda: {"module_id": module_id, "version": version, "kind": "application"})


def test_host_executor_starts_verified_modules_and_joins_only_core_and_worker(monkeypatch, tmp_path):
    from core.module import packages, trust
    from worker import application_container as containers
    apply = executor()
    (tmp_path / "config/application-modules").mkdir(parents=True)
    request = tmp_path / apply.MODULE_REQUEST
    request.write_text(json.dumps({"modules": [
        {"module_id": "brands", "package": {"ok": True}, "token": "t-brands"},
        {"module_id": "evil", "package": {"ok": False}, "token": "t-evil"}]}))
    monkeypatch.setattr(trust.TrustStore, "from_root", classmethod(lambda cls, root: None))

    def verify(package, _trust):
        if not package["ok"]:
            raise ValueError("bad signature")
        return manifest()
    monkeypatch.setattr(packages, "verify_package", verify)
    started, stopped, commands = [], [], []
    monkeypatch.setattr(containers, "start", lambda m, **options: started.append((m.module_id, options)))
    monkeypatch.setattr(containers, "stop", lambda module_id: stopped.append(module_id))

    def runner(command, **_):
        commands.append(command)
        if command[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout="old 1.0\n", returncode=0)
        if "ps" in command and "-q" in command:
            return SimpleNamespace(stdout=f"id-{command[-1]}\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    state = apply.apply_modules(tmp_path, runner)
    assert started == [("brands", {"core_url": "http://core:8080", "module_token": "t-brands", "host_port": None})]
    assert "old" in stopped  # no longer assigned
    assert ["docker", "network", "connect", "--alias", "core", "vertep-mod-brands", "id-core"] in commands
    assert ["docker", "network", "connect", "vertep-mod-brands", "id-worker"] in commands
    assert state["modules"] == {"brands": {"version": "0.0.4.16", "endpoint": "http://vertep-mod-brands:8080"}}
    assert "bad signature" in state["errors"]["evil"] and "evil" not in state["requested"]
    assert not request.exists()  # consumed, so PathExists does not loop
    saved = tmp_path / apply.MODULE_STATE
    assert json.loads(saved.read_text())["requested"] == {"brands": "0.0.4.16"}
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_host_container_has_no_host_port_and_stays_isolated():
    from worker.application_container import run_arguments
    arguments = run_arguments(manifest(), core_url="http://core:8080", module_token="t", host_port=None)
    assert not any(item.startswith("--publish") for item in arguments)
    assert "--network=vertep-mod-brands" in arguments and "--read-only" in arguments


def test_worker_asks_the_host_and_reports_the_module_it_runs(monkeypatch, tmp_path):
    from worker import application_supervisor as supervisor
    monkeypatch.setenv("VERTEP_APPLICATION_MODULES", "host")
    monkeypatch.setenv("VERTEP_APPLICATION_REQUEST_DIR", str(tmp_path))
    monkeypatch.setenv("VERTEP_APPLICATION_STATE", str(tmp_path / "worker-ports.json"))
    monkeypatch.setattr(supervisor, "_trust", lambda: None)
    monkeypatch.setattr(supervisor, "verify_package", lambda package, _trust: manifest())
    monkeypatch.setattr(supervisor, "_running", lambda: (_ for _ in ()).throw(AssertionError("no Docker in the Worker")))

    class Client:
        def post(self, url, json):
            if url.endswith("/node-assignments"):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"modules": [
                    {"module_id": "brands", "version": "0.0.4.16", "package": {"signed": 1}}]})
            return SimpleNamespace(status_code=200, json=lambda: {"token": "t-brands"})

    monkeypatch.setattr("httpx.get", lambda *a, **k: SimpleNamespace(json=lambda: {"ready": True}))
    inventory = supervisor.reconcile(Client(), "https://proxy:8443", "vertep")
    assert inventory[0]["ready"] is False  # the host has not started it yet
    request = json.loads((tmp_path / "request.json").read_text())
    assert request["modules"] == [{"module_id": "brands", "package": {"signed": 1}, "token": "t-brands"}]
    assert stat.S_IMODE((tmp_path / "request.json").stat().st_mode) == 0o600

    # The host consumed it and runs the module: the Worker reports it ready, asks nothing more.
    (tmp_path / "request.json").unlink()
    from datetime import datetime, timezone
    (tmp_path / "state.json").write_text(json.dumps({
        "modules": {"brands": {"version": "0.0.4.16", "endpoint": "http://vertep-mod-brands:8080"}},
        "requested": {"brands": "0.0.4.16"}, "updated_at": datetime.now(timezone.utc).isoformat()}))
    inventory = supervisor.reconcile(Client(), "https://proxy:8443", "vertep")
    assert inventory[0]["ready"] is True and inventory[0]["endpoint"] == "http://vertep-mod-brands:8080"
    assert not (tmp_path / "request.json").exists()
    assert supervisor.local_module("brands") == ("http://vertep-mod-brands:8080", "t-brands")


def test_production_worker_uses_the_host_executor_and_the_path_unit_watches_its_request():
    import yaml
    root = Path(__file__).parents[1]
    worker = yaml.safe_load((root / "deploy/docker-compose.yml").read_text())["services"]["worker"]
    assert worker["environment"]["VERTEP_APPLICATION_MODULES"] == "${VERTEP_APPLICATION_MODULES:-host}"
    assert not any("docker.sock" in str(item) for item in worker.get("volumes", []))
    assert "config/application-modules/request.json" in (root / "installer/vertep-deployment.path").read_text()


def test_runtime_bundle_ships_what_the_host_executor_imports():
    spec = importlib.util.spec_from_file_location("bundle", Path(__file__).parents[1] / "scripts/build-runtime-bundle.py")
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    assert bundle.FILES["worker/application_container.py"] == "worker/application_container.py"
    assert bundle.FILES["config/module-trust/publishers.json"] == "runtime/module-trust/publishers.json"


def test_host_executor_never_follows_a_link_the_worker_planted(tmp_path):
    import pytest
    apply = executor()
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    (tmp_path / "config/application-modules").mkdir(parents=True)
    (tmp_path / apply.MODULE_REQUEST).symlink_to(outside)
    with pytest.raises(ValueError, match="символьним"):
        apply.apply_modules(tmp_path, lambda *a, **k: None)
    assert outside.read_text() == "keep"
    state = tmp_path / apply.MODULE_STATE
    state.symlink_to(outside)
    apply._private_json(state, {"ok": True})
    assert outside.read_text() == "keep" and not state.is_symlink()
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
