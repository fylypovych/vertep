from typing import Any

from pydantic import BaseModel, Field, field_validator


class ScriptScene(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    video_prompt: str = Field(min_length=1, max_length=4000)
    voiceover: str = Field(default="", max_length=10000)
    duration: float = Field(default=5, gt=0, le=600)


class ScriptDocument(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=100)
    voiceover: str = Field(default="", max_length=50000)
    scenes: list[ScriptScene] = Field(min_length=1, max_length=200)
    platforms: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("hashtags", mode="before")
    @classmethod
    def normalize_hashtags(cls, value):
        if isinstance(value, str):
            return value.split()
        return value or []


def normalize_script(payload: dict, topic: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    value = dict(payload)
    value["title"] = str(value.get("title") or topic)
    raw_scenes = value.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("LLM response must contain at least one scene")
    scenes = []
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
    platform_metadata = {name: {"title": value["title"],
                                "description": str(value.get("description") or ""),
                                "hashtags": hashtags}
                         for name in ("youtube", "tiktok", "facebook", "instagram", "threads")}
    platform_metadata.update(value.get("platforms") or {})
    value["platforms"] = platform_metadata
    return ScriptDocument.model_validate(value).model_dump()
