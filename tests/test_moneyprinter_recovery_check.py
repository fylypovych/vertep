"""The pinned recovery check must fail on a duplicate upstream task or a false reconcile."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "moneyprinter-recovery-check.py"


def module():
    spec = importlib.util.spec_from_file_location("moneyprinter_recovery_check", SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class FakeRuntime:
    """Answers the container steps like a correct pinned runtime, with one switchable fault."""

    def __init__(self, recovery, fault=None):
        self.recovery, self.fault, self.tasks, self.restarted = recovery, fault, [], False

    def run(self, step, arguments=None):
        if step == self.recovery.PREPARE:
            return {"materials": [{"url": "a"}], "marker": "vertep-voice:x.wav", "tasks": []}
        if step == self.recovery.SUBMIT:
            first = not self.tasks
            if first or self.fault == "duplicate":
                self.tasks.append(f"task-{len(self.tasks)}")
            body = {"message": "success" if first else "reconciled", "data": {"task_id": "task-0"}}
            return {"status": 200, "body": body, "tasks": list(self.tasks)}
        if step == self.recovery.RECONCILE:
            if arguments["key"].startswith("unknown-"):
                return {"status": 404, "body": {"state": "unknown"}, "tasks": list(self.tasks)}
            lost = self.fault == "lost-after-restart" and self.restarted
            return {"status": 200, "tasks": list(self.tasks),
                    "body": {"state": "unknown" if lost else "submitted", "voice_staged": True,
                             "data": {} if lost else {"task_id": "task-0"}}}
        if step == self.recovery.TAMPER:
            return {"status": 503, "body": {"reason": "voice_staging_failed"},
                    "tasks_before": list(self.tasks), "tasks": list(self.tasks)}
        raise AssertionError(step)

    def restart(self):
        self.restarted = True


def run(fault=None):
    recovery = module()
    runtime = FakeRuntime(recovery, fault)
    recovery.time.sleep = lambda _seconds: None
    evidence = {}
    recovery.scenario(runtime, runtime.restart, evidence)
    return evidence


def test_a_correct_runtime_passes_every_recovery_step():
    evidence = run()
    assert evidence["assertions"] and all(item["passed"] for item in evidence["assertions"])


def test_a_duplicate_upstream_task_fails_the_check():
    with pytest.raises(AssertionError, match="created no upstream task"):
        run("duplicate")


def test_a_key_lost_across_the_restart_fails_the_check():
    with pytest.raises(AssertionError, match="after a runtime restart"):
        run("lost-after-restart")
