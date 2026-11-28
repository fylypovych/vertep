"""#553 AC3 (startup-recovery без повторного розгортання) та AC4 (операції й readiness)."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("startup_recovery_553", ROOT / "scripts" / "startup-recovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = _load()


def _root(tmp_path, services):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "deployment-plan.json").write_text(json.dumps({"services": services}))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    return tmp_path


def _runner(rows, calls, up_error=None):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(json.dumps(row) for row in rows()), stderr="")
        if "up" in cmd and up_error:
            raise up_error
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return run


def _row(service, state="running", health="healthy"):
    return {"Service": service, "State": state, "Health": health}


def test_a_healthy_running_stack_is_not_redeployed(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "compose_command", lambda root: ["docker", "compose"])
    calls = []
    root = _root(tmp_path, ["core", "postgres", "redis"])
    result = recovery.restart_containers(root, runner=_runner(lambda: [_row("core"), _row("postgres"), _row("redis", health="")], calls))
    assert result.startswith("stack already running and healthy")
    assert not any("up" in call for call in calls)


def test_only_the_missing_service_is_started(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "compose_command", lambda root: ["docker", "compose"])
    calls, state = [], {"up": False}

    def rows():
        return [_row("core"), _row("redis", "running" if state["up"] else "exited", "healthy" if state["up"] else "")]

    def run(cmd, **kwargs):
        if "up" in cmd:
            state["up"] = True
        return _runner(rows, calls)(cmd, **kwargs)

    root = _root(tmp_path, ["core", "redis"])
    result = recovery.restart_containers(root, runner=run, ready_timeout=2, poll_seconds=0)
    assert result == "recovered services: redis"
    assert ["docker", "compose", "up", "-d", "redis"] in calls


def test_a_failed_up_is_reported_as_an_error_not_a_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "compose_command", lambda root: ["docker", "compose"])
    calls = []
    root = _root(tmp_path, ["core", "redis"])
    timeout = subprocess.TimeoutExpired(["docker"], 180)
    result = recovery.restart_containers(
        root, runner=_runner(lambda: [_row("core")], calls, up_error=timeout), up_timeout=180)
    assert result.startswith("ERROR") and "timed out" in result


def test_services_that_never_become_ready_time_out_with_a_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "compose_command", lambda root: ["docker", "compose"])
    calls = []
    root = _root(tmp_path, ["core"])
    result = recovery.restart_containers(
        root, runner=_runner(lambda: [_row("core", "exited", "")], calls), ready_timeout=0, poll_seconds=0)
    assert result.startswith("ERROR")


def _update_state(state="SUCCEEDED", **extra):
    return {"action": "update", "request_id": "a" * 32, "state": state, "phase": "NORMAL",
            "current_version": "0.0.4.65", "available_version": "0.0.4.65", "update_available": False,
            "created_at": "2026-11-27T18:00:00+00:00", "updated_at": "2026-11-27T18:01:00+00:00", **extra}


def test_an_update_appears_once_in_operations_with_its_real_version(tmp_path):
    from core import operations
    first = operations.record_update_operation(_update_state(), state_dir=tmp_path)
    again = operations.record_update_operation(_update_state(), state_dir=tmp_path)
    assert first["operation_id"] == again["operation_id"]
    assert first["status"] == "COMPLETED" and first["target"] == "0.0.4.65"
    assert len(operations._read_all(tmp_path)) == 1


def test_progress_updates_the_same_operation_instead_of_adding_one(tmp_path):
    from core import operations
    running = operations.record_update_operation(_update_state("RUNNING", phase="INSTALLING"), state_dir=tmp_path)
    assert running["status"] == "RUNNING"
    done = operations.record_update_operation(_update_state(), state_dir=tmp_path)
    assert done["operation_id"] == running["operation_id"] and done["status"] == "COMPLETED"
    assert len(operations._read_all(tmp_path)) == 1


@pytest.fixture
def readiness(monkeypatch, tmp_path):
    from core import runtime
    from core.api import system
    from core.pipeline import JobStore
    from core.queue import TaskQueue
    store, queue = JobStore(root=str(tmp_path)), TaskQueue()
    queue._redis = None
    monkeypatch.setattr(system, "store", store)
    monkeypatch.setattr(system, "task_queue", queue)
    monkeypatch.setattr(system, "registered_nodes", lambda: [])
    monkeypatch.setattr(runtime, "_preparing", set())
    return system, store


def test_readiness_ignores_a_check_request_id_when_dispatch_is_not_paused(readiness, monkeypatch):
    system, store = readiness
    monkeypatch.setattr(system, "dispatch_allowed", lambda: True)
    monkeypatch.setattr(system, "get_system_state", lambda: {"operation_id": "check-request"})
    store.workers["vertep"] = {"node_name": "vertep", "status": "READY"}
    answer = system.update_readiness()
    assert answer["drain_operation_id"] is None and answer["queue_paused"] is False
    assert answer["unacknowledged_workers"] == [] or answer["ready"] is False


def test_an_idle_worker_that_acknowledged_the_current_drain_makes_it_ready(readiness, monkeypatch):
    system, store = readiness
    monkeypatch.setattr(system, "dispatch_allowed", lambda: False)
    monkeypatch.setattr(system, "get_system_state", lambda: {"operation_id": "op-1"})
    store.workers["vertep"] = {"node_name": "vertep", "status": "DRAINING", "drain_operation_id": "op-1"}
    answer = system.update_readiness()
    assert answer["ready"] is True and answer["unacknowledged_workers"] == []


def test_a_busy_stale_or_missing_worker_still_blocks_readiness(readiness, monkeypatch):
    system, store = readiness
    monkeypatch.setattr(system, "dispatch_allowed", lambda: False)
    monkeypatch.setattr(system, "get_system_state", lambda: {"operation_id": "op-2"})
    store.workers["busy"] = {"node_name": "busy", "status": "BUSY", "current_task": "t1"}
    store.workers["stale"] = {"node_name": "stale", "status": "DRAINING", "drain_operation_id": "op-1"}
    answer = system.update_readiness()
    assert answer["ready"] is False
    assert "stale" in answer["unacknowledged_workers"] and "busy" in answer["busy_workers"]
