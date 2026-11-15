"""i.0.0.4.80 (#480): structure metadata fails before scenes; prompt/duration semantics."""

import json

import pytest

from core.api.job_helpers.script import _script_task_for
from core.script_agent import STRUCTURE_STAGE, ScriptAgent, ScriptGenerationError

PLAN = [{"summary": "s1", "estimated_duration": 4}, {"summary": "s2", "estimated_duration": 6}]
SCENE = {"prompt": "p", "video_prompt": "v", "voiceover": "o", "duration": 5}


@pytest.fixture(autouse=True)
def _no_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)


class Recorder:
    def __init__(self, structure, scenes=None):
        self.structure = structure
        self.scenes = list(scenes or [])
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if STRUCTURE_STAGE in prompt:
            return json.dumps(self.structure)
        return json.dumps(self.scenes.pop(0) if self.scenes else SCENE)


def _structure(**extra):
    return {"title": "T", "description": "D", "hashtags": ["#a"], "voiceover": "V",
            "scenes_plan": PLAN, **extra}


@pytest.mark.parametrize("field,value", [
    ("title", "x" * 301),
    ("description", "x" * 5001),
    ("hashtags", ["#t"] * 101),
    ("hashtags", [{"tag": "x"}]),
    ("hashtags", {"a": 1}),
    ("voiceover", "x" * 50001),
    ("platforms", {"youtube": "not an object"}),
])
def test_invalid_metadata_fails_before_any_scene(field, value):
    llm = Recorder(_structure(**{field: value}))
    with pytest.raises(ScriptGenerationError, match="structure metadata is invalid"):
        ScriptAgent(llm).generate_script("тема")
    assert len(llm.prompts) == 1  # only the structure request was spent


@pytest.mark.parametrize("extra", [
    {},
    {"hashtags": "#a #b"},
    {"hashtags": []},
    {"description": ""},
    {"title": ""},  # falls back to the topic, as Assembly does
    {"platforms": {"youtube": {"title": "Y"}}},
])
def test_valid_metadata_is_kept(extra):
    llm = Recorder(_structure(**extra))
    script = ScriptAgent(llm).generate_script("тема")
    assert len(script["scenes"]) == 2
    expected = "тема" if extra.get("title") == "" else "T"
    assert script["title"] == expected


def test_null_duration_uses_the_planned_duration_without_retry():
    llm = Recorder(_structure(), scenes=[{**SCENE, "duration": None}, SCENE])
    script = ScriptAgent(llm, scene_attempts=3).generate_script("тема")
    assert len(llm.prompts) == 3  # structure + one request per scene, no regeneration
    assert script["scenes"][0]["duration"] == 4


def test_invalid_duration_is_still_regenerated_point_wise():
    llm = Recorder(_structure(), scenes=[{**SCENE, "duration": -1}, SCENE, SCENE])
    script = ScriptAgent(llm, scene_attempts=3).generate_script("тема")
    assert len(llm.prompts) == 4  # scene 1 regenerated once (F1), scene 2 once
    assert [scene["duration"] for scene in script["scenes"]] == [5, 5]


def test_missing_duration_uses_the_planned_duration():
    scene = {key: value for key, value in SCENE.items() if key != "duration"}
    llm = Recorder(_structure(), scenes=[scene, scene])
    assert [s["duration"] for s in ScriptAgent(llm).generate_script("тема")["scenes"]] == [4, 6]


def test_task_prompt_is_an_additional_brief_not_a_prompt_override():
    llm = Recorder(_structure())
    ScriptAgent(llm).generate_script("тема", brief="Зроби акцент на історії")
    assert all("Завдання: Зроби акцент на історії" in prompt for prompt in llm.prompts)
    assert STRUCTURE_STAGE in llm.prompts[0]  # the staged prompt is still built


def test_core_script_task_sets_no_prompt():
    job = type("Job", (), {"job_id": "j", "priority": 5, "topic": "t"})()
    task = _script_task_for(job, "system", {"name": "c"})
    assert "prompt" not in task
