"""i.0.0.4.74 (#474): one budget and cancel/lease checks for the staged script.

The production ``execute_script`` runs over a controlled HTTP transport (the
``migration_post`` the Worker uses) and a controlled clock: every LLM request
"takes" a fixed number of seconds. Deadline, cancellation, lease loss, scene
retry and the terminal result are checked without real time or network.
"""

import json

import pytest

import worker.role_executor as role_executor
from core import script_agent
from core.script_agent import (ScriptAgent, ScriptBudget, ScriptBudgetExceeded,
                               ScriptGenerationError, ScriptStopped)
from worker import service as worker_service

STRUCTURE = {"title": "T", "description": "D", "hashtags": ["#t"], "voiceover": "V",
             "scenes_plan": [{"summary": f"s{i}", "estimated_duration": 5} for i in (1, 2, 3)]}
SCENE = {"prompt": "p", "video_prompt": "v", "voiceover": "o", "duration": 5}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Transport:
    """Ollama stand-in: records each request and its timeout, advances the clock."""

    def __init__(self, clock, *, seconds=10.0, answers=None, on_request=None):
        self.clock = clock
        self.seconds = seconds
        self.requests: list[dict] = []
        self.answers = answers or {}
        self.on_request = on_request

    def __call__(self, _http, url, **kwargs):
        prompt = kwargs["json"]["prompt"]
        stage = "structure" if script_agent.STRUCTURE_STAGE in prompt else "scene"
        self.requests.append({"stage": stage, "timeout": kwargs["timeout"]})
        self.clock.now += self.seconds
        if self.on_request:
            self.on_request(len(self.requests))
        answer = self.answers.get(len(self.requests))
        if answer is None:
            answer = STRUCTURE if stage == "structure" else SCENE

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"response": answer if isinstance(answer, str) else json.dumps(answer)}

        return Response()


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(script_agent.time, "monotonic", value)
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("SCRIPT_SCENE_MAX_ATTEMPTS", "3")
    return value


def _task(**extra):
    return {"job_id": "job-1", "task_id": "task-1", "task": "script", "topic": "тема",
            "system_prompt": "", "character": {}, "timeout": 100, **extra}


def _run(monkeypatch, transport, task):
    monkeypatch.setattr(role_executor, "migration_post", transport)
    return role_executor.execute_script(task)


class TestBudget:
    def test_whole_run_within_budget_produces_the_script(self, clock, monkeypatch):
        transport = Transport(clock, seconds=10)
        [artifact] = _run(monkeypatch, transport, _task())
        assert artifact["kind"] == "script"
        assert [r["stage"] for r in transport.requests] == ["structure", "scene", "scene", "scene"]
        # Each transport timeout is cut to what is left of the one task budget.
        assert [r["timeout"] for r in transport.requests] == [100, 90, 80, 70]

    def test_exhausted_budget_starts_no_further_request(self, clock, monkeypatch):
        transport = Transport(clock, seconds=40)
        with pytest.raises(ScriptBudgetExceeded):
            _run(monkeypatch, transport, _task())
        # 0s → structure, 40s → scene 1, 80s → scene 2 (20s left), 120s → stop.
        assert [r["timeout"] for r in transport.requests] == [100, 60, 20]

    def test_per_request_timeout_is_never_longer_than_configured(self, clock, monkeypatch):
        monkeypatch.setenv("OLLAMA_SCRIPT_TIMEOUT", "300")
        budget = ScriptBudget(300, clock=clock)
        assert budget.bound(120) == 120
        clock.now += 250
        assert budget.bound(120) == 50

    def test_budget_failure_is_a_regular_failure_result(self, clock, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        monkeypatch.setattr(role_executor, "migration_post", Transport(clock, seconds=60))
        result = worker_service.execute_task(None, _task(), "text-1")
        assert result["success"] is False and not result.get("cancelled")
        assert "budget" in result["error"]


class TestOwnership:
    def test_cancel_stops_before_the_next_scene(self, clock, monkeypatch):
        task = _task()
        transport = Transport(clock, on_request=lambda n: n == 2 and task.update(cancelled=True))
        with pytest.raises(ScriptStopped, match="cancelled"):
            _run(monkeypatch, transport, task)
        assert [r["stage"] for r in transport.requests] == ["structure", "scene"]

    def test_lease_loss_stops_before_the_next_scene(self, clock, monkeypatch):
        task = _task()
        transport = Transport(clock, on_request=lambda n: n == 1 and task.update(lease_lost=True))
        with pytest.raises(ScriptStopped, match="lease lost"):
            _run(monkeypatch, transport, task)
        assert [r["stage"] for r in transport.requests] == ["structure"]

    def test_cancel_stops_before_a_scene_retry(self, clock, monkeypatch):
        task = _task()
        transport = Transport(clock, answers={2: "not json"},
                              on_request=lambda n: n == 2 and task.update(cancelled=True))
        with pytest.raises(ScriptStopped):
            _run(monkeypatch, transport, task)
        assert len(transport.requests) == 2  # the invalid scene is not regenerated

    def test_scene_retry_within_budget_still_recovers(self, clock, monkeypatch):
        transport = Transport(clock, answers={2: "not json", 3: {"prompt": ""}})
        [artifact] = _run(monkeypatch, transport, _task())
        assert artifact["kind"] == "script"
        assert len(transport.requests) == 6  # structure + scene1×3 + scene2 + scene3

    def test_cancelled_task_returns_a_terminal_cancelled_result(self, clock, monkeypatch):
        monkeypatch.setenv("NODE_ROLE", "text")
        task = _task()
        monkeypatch.setattr(role_executor, "migration_post",
                            Transport(clock, on_request=lambda n: task.update(cancelled=True)))
        result = worker_service.execute_task(None, task, "text-1")
        assert result["success"] is False and result["cancelled"] is True
        assert "artifacts" not in result

    def test_no_request_after_ownership_is_gone_at_claim(self, clock, monkeypatch):
        transport = Transport(clock)
        with pytest.raises(ScriptStopped):
            _run(monkeypatch, transport, _task(lease_lost=True))
        assert transport.requests == []


class TestAgentCheckpoint:
    @pytest.fixture(autouse=True)
    def _no_demo(self, monkeypatch):
        monkeypatch.delenv("DEMO_MODE", raising=False)

    def test_checkpoint_runs_before_every_request(self):
        calls = []
        answers = iter([json.dumps(STRUCTURE), "bad", json.dumps(SCENE),
                        json.dumps(SCENE), json.dumps(SCENE)])

        def complete(_prompt):
            calls.append("request")
            return next(answers)

        agent = ScriptAgent(complete, checkpoint=lambda: calls.append("check"))
        agent.generate_script("t")
        assert calls == ["check", "request"] * 5

    def test_invalid_scene_still_fails_after_its_attempts(self):
        answers = iter([json.dumps(STRUCTURE)] + ["bad"] * 3)
        agent = ScriptAgent(lambda _p: next(answers), scene_attempts=3, checkpoint=lambda: None)
        with pytest.raises(ScriptGenerationError, match="after 3 attempts"):
            agent.generate_script("t")


def test_worker_loop_marks_the_running_task_cancelled():
    """The Worker loop flags the task dict the executor holds (source contract)."""
    source = (role_executor.__file__.replace("role_executor.py", "service.py"))
    text = open(source, encoding="utf-8").read()
    assert 'active_task["cancelled"] = True' in text
    assert 'task["lease_lost"] = True' in text
