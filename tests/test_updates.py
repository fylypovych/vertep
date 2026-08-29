import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import _hash_secret, app
from core.update_manager import request_update, update_status


def load_update_agent():
    path = Path("scripts/update-agent.py").resolve()
    spec = importlib.util.spec_from_file_location("vertep_update_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_request_is_atomic_and_single_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    queued = request_update("check")
    assert queued["state"] == "PENDING"
    request_path = tmp_path / "requests" / f"{queued['request_id']}.json"
    assert json.loads(request_path.read_text(encoding="utf-8"))["action"] == "check"
    assert update_status()["pending"] == 1
    with pytest.raises(FileExistsError):
        request_update("update")
    assert not list(tmp_path.rglob("*.tmp"))


def test_web_update_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "false")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="disabled"):
        request_update("check")


def test_update_server_must_be_a_credential_free_https_origin(monkeypatch):
    from core.update_protocol import update_server_url

    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "https://update.vertep.ai")
    assert update_server_url() == "https://update.vertep.ai/"
    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "https://token:secret@example.com")
    with pytest.raises(RuntimeError, match="without credentials"):
        update_server_url()
    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "http://update.vertep.ai")
    with pytest.raises(RuntimeError, match="HTTPS"):
        update_server_url()


def test_update_agent_persists_signed_check_result_and_removes_request(monkeypatch, tmp_path):
    agent = load_update_agent()
    import core.update_protocol as protocol
    root = tmp_path / "repo"
    state = tmp_path / "state"
    root.mkdir()
    requests = state / "requests"
    requests.mkdir(parents=True)
    request_path = requests / ("a" * 32 + ".json")
    request_path.write_text(json.dumps({"request_id": "a" * 32, "action": "check"}), encoding="utf-8")
    monkeypatch.setattr(protocol, "fetch_manifest", lambda channel: {
        "version": "99.0.0", "required": False, "package": "packages/vertep.tar.gz",
        "sha256": "1" * 64, "signature": "signed"})
    monkeypatch.setattr(protocol, "validate_manifest", lambda manifest, public_key, **kwargs: manifest)
    agent.process_request(root, state, request_path)
    assert (state / "system-state.json").is_file()
    result = json.loads((state / "status.json").read_text(encoding="utf-8"))
    assert result["state"] == "SUCCEEDED"
    assert result["update_available"] is True
    assert result["available_version"] == "99.0.0"
    assert not request_path.exists()


def install_update(monkeypatch, tmp_path, gate_report: dict | None):
    """Drive a full install and return (status, executed commands); a missing report fails the gate."""
    agent = load_update_agent()
    import core.update_protocol as protocol
    root, state = tmp_path / "repo", tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    requests = state / "requests"
    requests.mkdir(parents=True)
    request_path = requests / ("b" * 32 + ".json")
    request_path.write_text(json.dumps({"request_id": "b" * 32, "action": "update"}), encoding="utf-8")
    monkeypatch.setattr(protocol, "fetch_manifest", lambda channel: {
        "version": "99.0.0", "required": False, "package": "packages/vertep.tar.gz",
        "sha256": "1" * 64, "signature": "signed"})
    monkeypatch.setattr(protocol, "validate_manifest", lambda manifest, public_key, **kwargs: manifest)
    monkeypatch.setattr(protocol, "download_package", lambda manifest, destination: destination)
    monkeypatch.setattr(agent, "wait_for_drain", lambda state_dir, status: None)
    commands: list[list[str]] = []

    def fake_run(command, cwd):
        commands.append(command)
        if command[1].endswith("health-gate.py"):
            report = Path(command[command.index("--report") + 1])
            if gate_report is None:
                raise RuntimeError("gate failed")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(gate_report), encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    agent.process_request(root, state, request_path)
    return json.loads((state / "status.json").read_text(encoding="utf-8")), commands


def test_successful_update_requires_the_final_health_gate(monkeypatch, tmp_path):
    status, commands = install_update(monkeypatch, tmp_path, {"passed": True, "failed": [], "checks": []})
    assert status["state"] == "SUCCEEDED"
    assert status["health"]["passed"] is True
    gate = next(command for command in commands if command[1].endswith("health-gate.py"))
    assert gate[gate.index("--mode") + 1] == "final"
    assert gate[gate.index("--expected-version") + 1] == "99.0.0"
    assert not any(command[-1] == "rollback" for command in commands)


def test_failed_final_health_gate_rolls_the_release_back(monkeypatch, tmp_path):
    status, commands = install_update(monkeypatch, tmp_path,
                                      {"passed": False, "failed": ["postgres"], "checks": []})
    assert status["state"] == "ROLLED_BACK"
    assert "postgres" in status["message"]
    assert any(command[-1] == "rollback" for command in commands)


def test_unreadable_health_report_rolls_the_release_back(monkeypatch, tmp_path):
    status, commands = install_update(monkeypatch, tmp_path, None)
    assert status["state"] == "ROLLED_BACK"
    assert any(command[-1] == "rollback" for command in commands)


def test_update_api_requires_admin_role(monkeypatch, tmp_path):
    password = "operator-password-long"
    monkeypatch.setenv("ADMIN_PASSWORD", "fallback-admin-password")
    monkeypatch.setenv("USERS_JSON", json.dumps({"operator": {
        "password_hash": _hash_secret(password), "role": "operator"}}))
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    client = TestClient(app)
    assert client.get("/api/system/update", auth=("operator", password)).status_code == 200
    assert client.post("/api/system/update/check", auth=("operator", password)).status_code == 403
    queued = client.post("/api/system/update/check", auth=("admin", "fallback-admin-password"))
    assert queued.status_code == 200
    assert queued.json()["state"] == "PENDING"
