"""i.0.0.4.5: recovery uses the active release and never claims NORMAL without proof."""
import importlib.util
import json
from pathlib import Path

import pytest


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = _load("startup_recovery_405", "scripts/startup-recovery.py")
watchdog = _load("watchdog_405", "scripts/watchdog.py")
agent = _load("update_agent_405", "scripts/update-agent.py")


def _release(tmp_path, monkeypatch):
    # The symlink itself needs privileges on Windows; the selection rule does not.
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "current")
    return tmp_path / "current"


@pytest.mark.parametrize("module", [recovery, watchdog])
def test_compose_comes_from_the_active_release(tmp_path, monkeypatch, module):
    current = _release(tmp_path, monkeypatch)
    assert str(current / "docker-compose.yml") in module.compose_command(tmp_path)


@pytest.mark.parametrize("module", [recovery, watchdog])
def test_compose_without_release_layout_uses_the_root(tmp_path, module):
    assert str(tmp_path / "docker-compose.yml") in module.compose_command(tmp_path)


@pytest.mark.parametrize("phase", ["UPDATING", "RESTARTING", "VERIFYING", "RECOVERING"])
def test_every_interrupted_phase_is_rolled_back(tmp_path, monkeypatch, phase):
    (tmp_path / "status.json").write_text(json.dumps({"state": "RUNNING", "phase": phase}))
    monkeypatch.setattr(recovery, "run", lambda *args, **kwargs: "ok")
    assert recovery.recover_interrupted_update(tmp_path, tmp_path) is True
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "ROLLED_BACK"


def test_finished_update_is_left_alone(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"state": "SUCCEEDED", "phase": "NORMAL"}))
    assert recovery.recover_interrupted_update(tmp_path, tmp_path) is False


def _database(monkeypatch, state):
    from core import system_state
    written = []
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "postgres")
    monkeypatch.setattr(system_state, "_database_state", lambda: state)
    monkeypatch.setattr(system_state, "set_system_state",
                        lambda value, reason, operation, state_dir: written.append(value))
    return written


def test_database_restore_fence_survives_startup(tmp_path, monkeypatch):
    written = _database(monkeypatch, {"state": "MAINTENANCE", "operation_id": "restore:snap"})
    recovery.reconcile_database_state(tmp_path)
    assert written == []


def test_database_transitional_state_returns_to_normal(tmp_path, monkeypatch):
    written = _database(monkeypatch, {"state": "UPDATING", "operation_id": "update-1"})
    recovery.reconcile_database_state(tmp_path)
    assert written == ["NORMAL"]


def test_local_emergency_reaches_the_database(tmp_path, monkeypatch):
    written = _database(monkeypatch, {"state": "UPDATING", "operation_id": "update-1"})
    recovery.mark_emergency(tmp_path, "rollback failed")
    recovery.reconcile_database_state(tmp_path)
    assert written == ["EMERGENCY"]


def test_unreachable_database_changes_nothing(tmp_path, monkeypatch):
    written = _database(monkeypatch, None)
    from core import system_state

    def down():
        raise OSError("down")

    monkeypatch.setattr(system_state, "_database_state", down)
    recovery.reconcile_database_state(tmp_path, attempts=1)
    assert written == []


def test_rollback_health_is_polled_until_core_answers(monkeypatch):
    answers = [OSError("down"), {"status": "HEALTHY"}]

    def core_json(path):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(agent, "core_json", core_json)
    monkeypatch.setenv("UPDATE_ROLLBACK_HEALTH_POLL_SECONDS", "0")
    agent.verify_restored_release()
    assert answers == []


def test_unhealthy_rollback_ends_in_emergency_not_normal(tmp_path, monkeypatch):
    def core_json(path):
        raise OSError("down")

    states = []
    monkeypatch.setattr(agent, "core_json", core_json)
    monkeypatch.setattr(agent, "run", lambda *args, **kwargs: "rollback output")
    monkeypatch.setattr(agent, "_record_system_state",
                        lambda state, value, *args: states.append(value.value))
    monkeypatch.setenv("UPDATE_ROLLBACK_HEALTH_TIMEOUT_SECONDS", "0")
    state = {"request_id": "r", "log": []}
    agent._attempt_rollback(tmp_path, tmp_path, state)
    assert state["phase"] == "EMERGENCY"
    assert states == ["EMERGENCY"]


def test_core_block_has_no_stale_backup_commands():
    compose = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "BACKUP_PG_RESTORE_CMD" not in compose
    assert "BGSAVE" not in compose


def test_worker_rollback_does_not_depend_on_core_health(monkeypatch, tmp_path):
    def core_json(path):
        raise OSError("CORE is down")

    (tmp_path / ".env").write_text("ROLE=worker\n", encoding="utf-8")
    monkeypatch.delenv("ROLE", raising=False)
    monkeypatch.delenv("NODE_ROLE", raising=False)
    monkeypatch.setenv("VERTEP_ROOT", str(tmp_path))
    monkeypatch.setattr(agent, "core_json", core_json)
    agent.verify_restored_release()


def test_core_address_comes_from_the_installation_env(monkeypatch, tmp_path):
    for name in ("VERTEP_CORE_URL", "CORE_URL", "CORE_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("VERTEP_CORE_URL=http://127.0.0.1:18080\n", encoding="utf-8")
    monkeypatch.setenv("VERTEP_ROOT", str(tmp_path))
    assert agent._core_url() == "http://127.0.0.1:18080"
