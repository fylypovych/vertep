"""i.0.0.4.9 / i.0.0.4.10: rollback keeps safe data; update-agent.py is the only executor."""
import importlib.util
from pathlib import Path

import pytest

from core.system_state import STATE_OPERATION_POLICY, SystemState, jobs_may_be_created, set_system_state

spec = importlib.util.spec_from_file_location("update_agent_409", "scripts/update-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


@pytest.mark.parametrize("state,allowed", [
    (SystemState.NORMAL, True), (SystemState.MAINTENANCE, True), (SystemState.UPDATING, False),
    (SystemState.RECOVERING, False), (SystemState.READ_ONLY, False), (SystemState.EMERGENCY, False)])
def test_job_creation_follows_the_backup_boundary(tmp_path, monkeypatch, state, allowed):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    set_system_state(state, "test", None, tmp_path)
    assert jobs_may_be_created() is allowed
    assert ("create_job" in STATE_OPERATION_POLICY[state]) is allowed


@pytest.mark.parametrize("manifest,flag", [
    ({"database": {"rollback_safe": True}}, "true"),
    ({"database": {"rollback_safe": False}}, "false"),
    ({"database": {"rollback_safe": "true"}}, "false"),
    ({"database": None}, "false"),
    ({}, "false")])
def test_only_an_explicit_promise_keeps_data(manifest, flag):
    assert agent.rollback_safe_flag(manifest) == flag


def test_safe_release_does_not_mark_the_database_for_restore():
    script = Path("scripts/vertep").read_text(encoding="utf-8")
    assert '[[ "${VERTEP_ROLLBACK_SAFE:-false}" == true ]] || touch "$backup/database-restore-required"' in script
    # DROP SCHEMA stays behind the marker.
    assert script.index('-f "$backup/database-restore-required"') < script.index("DROP SCHEMA public")


@pytest.mark.parametrize("role,skip", [
    ("worker", True), ("gpu", True), ("core", False), ("core-worker", False), ("unknown", False), ("", False)])
def test_only_core_nodes_wait_for_drain(monkeypatch, role, skip):
    monkeypatch.setenv("ROLE", role)
    monkeypatch.delenv("NODE_ROLE", raising=False)
    assert agent.drain_is_coordinated_elsewhere() is skip


def test_legacy_worker_executor_is_gone():
    assert not Path("scripts/worker_update.py").exists()
    for name in ("scripts/build-runtime-bundle.py", "scripts/release-bundle.py", "bootstrap.sh"):
        text = Path(name).read_text(encoding="utf-8")
        assert "worker_update.py" not in text and "vertep-worker-update" not in text
