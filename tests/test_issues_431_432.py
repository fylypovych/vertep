"""i.0.0.4.31 GPU failure is reported, not replaced; i.0.0.4.32 turns between identical workers."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import turns
from core.api import tasks
from core.models import TaskClaim, TaskResult

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()


def _worker(**fields):
    return {"role": "gpu", "capabilities": ["image_generation"], "vram_mb": 8192, "status": "READY",
            "last_seen": datetime.fromtimestamp(NOW, timezone.utc).isoformat(), **fields}


@pytest.fixture(autouse=True)
def _clean():
    turns._yielding_since.clear()


def test_identical_workers_take_turns():
    workers = {"a": _worker(), "b": _worker(), "c": _worker()}
    order = []
    for step in range(6):
        for name in "abc":
            if turns.may_claim_now(name, workers, NOW):
                turns.record_claim(name, workers)
                workers[name]["scheduler_last_claim_at"] = NOW - 1000 + step * 10 + "abc".index(name)
                order.append(name)
                break
    assert order == ["a", "b", "c", "a", "b", "c"]


@pytest.mark.parametrize("other", [
    {"status": "BUSY"}, {"current_task": "t"}, {"status": "OFFLINE"}, {"desired_state": "DRAINING"},
    {"last_seen": "2026-11-05T00:00:00+00:00"}, {"vram_mb": 24576}, {"capabilities": ["tts"]},
    {"scheduler_tags": ["fast"]}])
def test_a_busy_absent_or_different_worker_never_holds_a_task_back(other):
    workers = {"a": _worker(scheduler_last_claim_at=NOW), "b": _worker(**other)}
    assert turns.may_claim_now("a", workers, NOW)


def test_only_worker_claims_without_delay():
    assert turns.may_claim_now("a", {"a": _worker(scheduler_last_claim_at=NOW)}, NOW)


def test_turn_timeout_comes_from_the_panel_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(turns, "config_root", lambda: tmp_path)
    assert turns.turn_timeout_seconds() == 45
    assert turns.save_turn_timeout_seconds(10) == 10
    workers = {"a": _worker(scheduler_last_claim_at=NOW), "b": _worker()}
    assert not turns.may_claim_now("a", workers, NOW)
    assert turns.may_claim_now("a", workers, NOW + 10)
    for bad in (-1, 601, "10", True, None):
        with pytest.raises(ValueError):
            turns.save_turn_timeout_seconds(bad)


def test_a_hung_worker_cannot_hold_work_forever(monkeypatch, tmp_path):
    monkeypatch.setattr(turns, "config_root", lambda: tmp_path)
    workers = {"a": _worker(scheduler_last_claim_at=NOW), "b": _worker()}
    assert not turns.may_claim_now("a", workers, NOW)
    assert not turns.may_claim_now("a", workers, NOW + 30)
    assert turns.may_claim_now("a", workers, NOW + 45)


def test_after_a_core_restart_the_round_starts_again():
    workers = {"a": _worker(), "b": _worker()}
    assert turns.may_claim_now("a", workers, NOW) and turns.may_claim_now("b", workers, NOW)


def test_claim_route_makes_the_worker_wait(monkeypatch):
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, req: True)
    fresh = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(tasks.store, "workers", {"a": _worker(scheduler_last_claim_at=1.0, last_seen=fresh),
                                                 "b": _worker(last_seen=fresh)})
    answer = tasks.claim_task(TaskClaim(node_name="a", vram_mb=0), SimpleNamespace(headers={}))
    assert answer == {"task": None, "worker_state": "WAITING_FOR_TURN"}


def test_priority_no_longer_chooses_the_node():
    assert "scheduler_priority" not in Path("core/dispatcher.py").read_text(encoding="utf-8")


def _result(success, error=None, cancelled=False):
    return TaskResult(job_id="j", node_name="gpu-1", task_id="t", success=success, error=error, cancelled=cancelled)


@pytest.fixture
def alerts(monkeypatch):
    class Store:
        def __init__(self):
            self.recorded, self.resolved = [], []

        def record(self, alert):
            self.recorded.append(alert)

        def resolve(self, match, reason):
            self.resolved.append(match)

    store = Store()
    monkeypatch.setattr(tasks, "get_alert_store", lambda: store)
    monkeypatch.setattr(tasks.task_queue, "find", lambda task_id: {"task_type": "image"})
    return store


JOB = SimpleNamespace(image_storyboard_task_ids=[])


def test_gpu_failure_tells_the_operator_to_restart(alerts):
    tasks._report_gpu_outcome(JOB, _result(False, "Allowed GPU providers exhausted: primary: failed with cleanup confirmed"))
    alert = alerts.recorded[0]
    assert alert["severity"] == "error" and alert["details"]["provider"] == "primary"
    assert "primary" in alert["message"] and "gpu-1" in alert["message"] and "перезапуск" in alert["message"]


def test_gpu_success_resolves_and_cancel_is_silent(alerts):
    tasks._report_gpu_outcome(JOB, _result(True))
    assert alerts.resolved and not alerts.recorded
    tasks._report_gpu_outcome(JOB, _result(False, "stopped", cancelled=True))
    assert not alerts.recorded


def test_other_tasks_raise_no_gpu_alert(alerts, monkeypatch):
    monkeypatch.setattr(tasks.task_queue, "find", lambda task_id: {"task": "script"})
    tasks._report_gpu_outcome(JOB, _result(False, "boom"))
    assert not alerts.recorded and not alerts.resolved


def test_router_promises_no_fallback():
    source = Path("core/gpu/router.py").read_text(encoding="utf-8")
    assert "route_with_fallback" not in source and "cooldown" not in source
