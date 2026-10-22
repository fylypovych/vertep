"""Тести для modules/text/text_logic.py — чиста бізнес-логіка Text Module."""

from modules.text.text_logic import (
    build_text_prompt,
    default_script_scenes,
    extract_voiceover,
    normalize_script_data,
    validate_script_document,
)


def test_normalize_script_data_basic():
    payload = {
        "title": "Тест",
        "scenes": [{"prompt": "scene 1", "video_prompt": "vp 1", "duration": 5}],
    }
    result = normalize_script_data(payload, "Тема")
    assert result["title"] == "Тест"
    assert len(result["scenes"]) == 1
    assert result["scenes"][0]["prompt"] == "scene 1"
    assert "youtube" in result["platforms"]
    assert "tiktok" in result["platforms"]


def test_normalize_script_data_missing_title_uses_topic():
    payload = {"scenes": [{"prompt": "scene 1"}]}
    result = normalize_script_data(payload, "Моя тема")
    assert result["title"] == "Моя тема"


def test_normalize_script_data_empty_scenes_raises():
    try:
        normalize_script_data({"scenes": []}, "Тема")
    except ValueError:
        pass
    else:
        raise AssertionError("empty scenes should raise ValueError")


def test_normalize_script_data_non_dict_raises():
    try:
        normalize_script_data("not a dict", "Тема")
    except ValueError:
        pass
    else:
        raise AssertionError("non-dict should raise ValueError")


def test_validate_script_document_valid():
    doc = {"title": "Test", "scenes": [{"prompt": "p", "duration": 5}]}
    assert validate_script_document(doc) == []


def test_validate_script_document_missing_title():
    doc = {"scenes": [{"prompt": "p"}]}
    errors = validate_script_document(doc)
    assert "title is required" in errors


def test_validate_script_document_no_scenes():
    doc = {"title": "Test"}
    errors = validate_script_document(doc)
    assert "at least one scene is required" in errors


def test_validate_script_document_scene_no_prompt():
    doc = {"title": "Test", "scenes": [{"duration": 5}]}
    errors = validate_script_document(doc)
    assert any("has no prompt" in e for e in errors)


def test_build_text_prompt_with_system_and_character():
    prompt = build_text_prompt(
        "Тема", system_prompt="Система",
        character={"visual": {"style": "аніме"}, "generation": {"workflow": "wf.json"}},
    )
    assert "Система" in prompt
    assert "Візуальний стиль: аніме" in prompt
    assert "Workflow: wf.json" in prompt
    assert "Тема" in prompt


def test_build_text_prompt_minimal():
    prompt = build_text_prompt("Просто тема")
    assert prompt == "Просто тема"


def test_extract_voiceover_from_scene():
    data = {"scenes": [{"voiceover": "озвучка сцени"}]}
    assert extract_voiceover(data, 0) == "озвучка сцени"


def test_extract_voiceover_fallback_to_script():
    data = {"voiceover": "загальна озвучка", "scenes": [{} ]}
    assert extract_voiceover(data, 0) == "загальна озвучка"


def test_default_script_scenes():
    scenes = default_script_scenes("Тема")
    assert len(scenes) == 1
    assert scenes[0]["prompt"] == "Тема"
    assert scenes[0]["duration"] == 5
