"""Issue #465 (i.0.0.4.65): production ``execute_script`` runs Structure -> Scenes -> Assembly.

Only the Ollama HTTP transport is replaced; the Worker path is exercised
through ``execute_role_task`` with ``DEMO_MODE`` disabled.
"""

import base64
import json
import re

import httpx
import pytest

from worker import role_executor


class Response:
    def __init__(self, value, status=200):
        self.value = value
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://ollama/api/generate")
            raise httpx.HTTPStatusError("backend failed", request=request,
                                        response=httpx.Response(self.status_code, request=request))

    def json(self):
        return self.value


STRUCTURE = {"title": "Козацька історія", "description": "опис", "hashtags": ["#vertep"],
             "voiceover": "загальна озвучка",
             "scenes_plan": [{"summary": "степ на світанку", "estimated_duration": 4},
                             {"summary": "козак на коні", "estimated_duration": 6},
                             {"summary": "фінал біля Дніпра", "estimated_duration": 5}]}


def scene(number):
    return {"prompt": f"image {number}", "video_prompt": f"video {number}",
            "voiceover": f"озвучка {number}", "duration": 4 + number}


class FakeOllama:
    """Answers per stage; ``answers`` maps a stage key to a queue of answers."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def stage(self, prompt):
        if "STAGE=structure" in prompt:
            return "structure"
        return "scene-" + re.search(r"SCENE_INDEX=(\d+)", prompt).group(1)

    def __call__(self, endpoint, json=None, timeout=None, **kwargs):
        key = self.stage(json["prompt"])
        self.calls.append({"stage": key, "endpoint": endpoint, "payload": json, "timeout": timeout})
        queue = self.answers.get(key)
        if queue:
            answer = queue.pop(0)
            if isinstance(answer, Response):
                return answer
            return Response({"response": answer})
        if key == "structure":
            return Response({"response": _json(STRUCTURE)})
        return Response({"response": _json(scene(int(key.split("-")[1])))})


def _json(value):
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def production(monkeypatch):
    import adapters.providers as providers_module
    monkeypatch.setattr(providers_module, "_providers", None)
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("OLLAMA_SCRIPT_MODEL", "script-model")
    monkeypatch.delenv("SCRIPT_SCENE_MAX_ATTEMPTS", raising=False)

    def install(fake):
        monkeypatch.setattr(role_executor.httpx, "post", fake)
        return fake
    return install


def task(**extra):
    value = {"task": "script", "topic": "козаки", "system_prompt": "Ти — оповідач.",
             "character": {"visual": {"style": "акварель", "aspect_ratio": "9:16"}}}
    value.update(extra)
    return value


def run(value):
    [artifact] = role_executor.execute_role_task("text", value)
    assert artifact["filename"] == "script.json" and artifact["kind"] == "script"
    return json.loads(base64.b64decode(artifact["data_base64"]).decode("utf-8"))


def test_stages_run_in_order_and_assemble_valid_script(production):
    fake = production(FakeOllama())
    script = run(task(revision="коротше"))
    assert [call["stage"] for call in fake.calls] == ["structure", "scene-1", "scene-2", "scene-3"]
    assert all(call["endpoint"] == "http://ollama:11434/api/generate" for call in fake.calls)
    assert all(call["payload"]["model"] == "script-model" for call in fake.calls)
    assert all(call["payload"]["format"] == "json" for call in fake.calls)
    prompts = [call["payload"]["prompt"] for call in fake.calls]
    # Character config and revision reach every stage.
    assert all("Ти — оповідач." in p and "акварель" in p and "9:16" in p for p in prompts)
    assert all("REVISION INSTRUCTION: коротше" in p for p in prompts)
    assert "козак на коні" in prompts[2]
    from core.script_schema import ScriptDocument
    ScriptDocument.model_validate(script)
    assert script["title"] == "Козацька історія"
    assert [s["prompt"] for s in script["scenes"]] == ["image 1", "image 2", "image 3"]
    assert script["platforms"]["youtube"]["title"] == "Козацька історія"


def test_invalid_scene_is_regenerated_alone(production):
    fake = production(FakeOllama({"scene-2": ["not json", _json({"prompt": "", "duration": 3})]}))
    script = run(task())
    stages = [call["stage"] for call in fake.calls]
    assert stages == ["structure", "scene-1", "scene-2", "scene-2", "scene-2", "scene-3"]
    assert [s["prompt"] for s in script["scenes"]] == ["image 1", "image 2", "image 3"]


def test_scene_invalid_after_attempts_fails_without_demo_result(production, monkeypatch):
    monkeypatch.setenv("SCRIPT_SCENE_MAX_ATTEMPTS", "2")
    fake = production(FakeOllama({"scene-1": [_json({"duration": -1}), "{}"]}))
    with pytest.raises(RuntimeError, match="Scene 1 stayed invalid after 2 attempts"):
        role_executor.execute_role_task("text", task())
    assert [call["stage"] for call in fake.calls] == ["structure", "scene-1", "scene-1"]


@pytest.mark.parametrize("answer", ["", "not json", _json({"title": "x"}), _json({"scenes_plan": []})])
def test_invalid_structure_fails(production, answer):
    fake = production(FakeOllama({"structure": [answer]}))
    with pytest.raises(RuntimeError):
        role_executor.execute_role_task("text", task())
    assert [call["stage"] for call in fake.calls] == ["structure"]


def test_backend_error_propagates_and_is_not_retried_as_invalid_scene(production):
    fake = production(FakeOllama({"scene-2": [Response({}, status=500)]}))
    with pytest.raises(httpx.HTTPStatusError):
        role_executor.execute_role_task("text", task())
    assert [call["stage"] for call in fake.calls] == ["structure", "scene-1", "scene-2"]


def test_each_stage_uses_script_timeout(production):
    fake = production(FakeOllama())
    run(task(timeout=42))
    assert {call["timeout"] for call in fake.calls} == {42}
