import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


class CharacterConfig(BaseModel):
    id: str = Field(pattern=SAFE_ID.pattern)
    name: str = Field(min_length=1, max_length=120)
    language: str = Field(default="uk", min_length=2, max_length=12)
    enabled: bool = True
    workflow: str | None = None
    system_prompt: str = ""
    voice: dict = Field(default_factory=dict)
    visual: dict = Field(default_factory=dict)
    generation: dict = Field(default_factory=dict)
    publishing: dict = Field(default_factory=dict)


class BrandConfig(BaseModel):
    id: str = Field(pattern=SAFE_ID.pattern)
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)
    publishing: dict = Field(default_factory=dict)


def read_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def load_character(root: Path, character_id: str) -> CharacterConfig:
    directory = root / character_id
    base = read_json(directory / "character.json")
    base.update({
        "system_prompt": (directory / "system_prompt.txt").read_text(encoding="utf-8") if (directory / "system_prompt.txt").exists() else "",
        "voice": read_json(directory / "voice.json"),
        "visual": read_json(directory / "visual.json"),
        "generation": read_json(directory / "generation.json"),
        "publishing": read_json(directory / "publishing.json"),
    })
    return CharacterConfig.model_validate(base)


def save_character(root: Path, config: CharacterConfig) -> None:
    directory = root / config.id
    directory.mkdir(parents=True, exist_ok=True)
    base = config.model_dump(include={"id", "name", "language", "enabled", "workflow"}, exclude_none=True)
    (directory / "character.json").write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "system_prompt.txt").write_text(config.system_prompt, encoding="utf-8")
    for name in ("voice", "visual", "generation", "publishing"):
        (directory / f"{name}.json").write_text(json.dumps(getattr(config, name), ensure_ascii=False, indent=2), encoding="utf-8")
