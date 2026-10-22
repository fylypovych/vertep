"""Чиста бізнес-логіка Text Module.

Не залежить від CORE, адаптерів або зовнішніх сервісів.  Лише перетворення
даних, валідація та підготовка підказок для LLM-провайдера (коє Викликається
з ``text_module.py``).
"""

from __future__ import annotations

from typing import Any


def normalize_script_data(payload: dict[str, Any], topic: str) -> dict[str, Any]:
    """Нормалізувати сирий payload сценарію з LLM у канонічний вигляд.

    Не викликає LLM, не робить HTTP-запитів.  Помилки викликають ValueError.
    """
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    value = dict(payload)
    value["title"] = str(value.get("title") or topic)
    raw_scenes = value.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("LLM response must contain at least one scene")
    scenes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_scenes):
        if not isinstance(raw, dict):
            raise ValueError(f"Scene {index + 1} must be an object")
        scene = dict(raw)
        prompt = scene.get("prompt") or scene.get("image_prompt")
        if not prompt:
            raise ValueError(f"Scene {index + 1} has no image prompt")
        scene["prompt"] = str(prompt)
        scene["video_prompt"] = str(scene.get("video_prompt") or prompt)
        if not scene.get("voiceover") and not scene.get("text") and index == 0:
            scene["voiceover"] = str(value.get("voiceover") or "")
        elif not scene.get("voiceover") and scene.get("text"):
            scene["voiceover"] = str(scene["text"])
        scenes.append(scene)
    value["scenes"] = scenes
    hashtags = value.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = hashtags.split()
    platform_metadata = {
        name: {"title": value["title"],
               "description": str(value.get("description") or ""),
               "hashtags": hashtags}
        for name in ("youtube", "tiktok", "facebook", "instagram", "threads")
    }
    platform_metadata.update(value.get("platforms") or {})
    value["platforms"] = platform_metadata
    return value


def validate_script_document(doc: dict[str, Any]) -> list[str]:
    """Перевірити нормалізований сценарій.  Повертає список помилок (порожній — валідний)."""
    errors: list[str] = []
    title = doc.get("title")
    if not title or not str(title).strip():
        errors.append("title is required")
    scenes = doc.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("at least one scene is required")
    else:
        for index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                errors.append(f"scene {index + 1} must be an object")
                continue
            if not scene.get("prompt"):
                errors.append(f"scene {index + 1} has no prompt")
            duration = scene.get("duration", 5)
            try:
                duration_value = float(duration)
                if duration_value <= 0 or duration_value > 600:
                    errors.append(f"scene {index + 1} duration out of range")
            except (TypeError, ValueError):
                errors.append(f"scene {index + 1} has invalid duration")
    return errors


def build_text_prompt(topic: str, system_prompt: str = "", character: dict[str, Any] | None = None) -> str:
    """Збудувати підказку для LLM-генерації сценарію.  Чиста функція рядків."""
    parts: list[str] = []
    if system_prompt:
        parts.append(str(system_prompt).strip())
    if character:
        style = (character or {}).get("visual", {}).get("style") or (character or {}).get("style")
        if style:
            parts.append(f"Візуальний стиль: {style}")
        generation = (character or {}).get("generation", {})
        if generation and generation.get("workflow"):
            parts.append(f"Workflow: {generation['workflow']}")
    parts.append(str(topic).strip())
    return "\n\n".join(parts)


def extract_voiceover(script_data: dict[str, Any], scene_index: int = 0) -> str:
    """Отримати текст озвучки для сцени із нормалізованого сценарію."""
    scenes = script_data.get("scenes") or []
    if not scenes:
        return str(script_data.get("voiceover") or "")
    scene = scenes[scene_index] if isinstance(scenes[scene_index], dict) else {}
    voiceover = scene.get("voiceover") or scene.get("text") or ""
    if not voiceover:
        voiceover = script_data.get("voiceover") or ""
    return str(voiceover)


def default_script_scenes(topic: str) -> list[dict[str, Any]]:
    """Дефолтні сцени, коли LLM не повернув валідний результат."""
    return [{"prompt": topic, "video_prompt": topic, "voiceover": topic, "duration": 5}]
