import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


class DefaultCharacterError(ValueError):
    """No usable default character is configured (i.0.0.4.75)."""


def default_character_id(*preferred: str) -> str:
    """The configured default character: no character id lives in runtime code.

    ``preferred`` names more specific settings (e.g. ``TELEGRAM_DEFAULT_CHARACTER``)
    checked before ``DEFAULT_CHARACTER``. The first one that is set must name an
    existing character in ``CHARACTERS_ROOT``; an invalid value is an error, it
    never falls through to another character silently.
    """
    root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    for name in (*preferred, "DEFAULT_CHARACTER"):
        value = os.getenv(name, "").strip()
        if not value:
            continue
        if not SAFE_ID.fullmatch(value) or not (root / value / "character.json").is_file():
            raise DefaultCharacterError(
                f"Configured default character {name}={value!r} does not exist")
        missing = missing_character_files(root, value)
        if missing:
            raise DefaultCharacterError(
                f"Configured default character {name}={value!r} is incomplete: missing {', '.join(missing)}")
        return value
    raise DefaultCharacterError(
        "No default character is configured (DEFAULT_CHARACTER); choose a character explicitly")


import secrets
import string


def generate_character_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))


class CharacterConfig(BaseModel):
    id: str | None = Field(default=None, pattern=SAFE_ID.pattern)
    name: str = Field(min_length=1, max_length=120)
    language: str = Field(default="uk", min_length=2, max_length=12)
    enabled: bool = True
    workflow: str | None = None
    system_prompt: str = ""
    voice: dict = Field(default_factory=dict)
    visual: dict = Field(default_factory=dict)
    generation: dict = Field(default_factory=dict)
    publishing: dict = Field(default_factory=dict)

    def model_post_init(self, __context) -> None:
        if self.id is None:
            object.__setattr__(self, 'id', generate_character_id())


class BrandConfig(BaseModel):
    id: str = Field(pattern=SAFE_ID.pattern)
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)
    publishing: dict = Field(default_factory=dict)
    # #502 AC3: brand publication policy (channels may override it).
    publication_policy: str = Field(default="approval_required", pattern="^(automatic|approval_required)$")


def read_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


# AGENTS.md §3.2: a character is ready only with its complete configuration.
REQUIRED_CHARACTER_FILES = ("character.json", "system_prompt.txt", "voice.json",
                            "visual.json", "generation.json", "publishing.json")


class IncompleteCharacterError(ValueError):
    """A character directory lacks required configuration files (i.0.0.4.76)."""


def missing_character_files(root: Path, character_id: str) -> list[str]:
    directory = Path(root) / character_id
    return [name for name in REQUIRED_CHARACTER_FILES if not (directory / name).is_file()]


def load_character(root: Path, character_id: str, *, require_complete: bool = False) -> CharacterConfig:
    """Load a character; ``require_complete`` refuses one missing a §3.2 file.

    The standard paths that announce a character as ready (character listing,
    default selection, Telegram selection) require completeness; an explicit
    id keeps loading as before.
    """
    directory = root / character_id
    if require_complete:
        missing = missing_character_files(root, character_id)
        if missing:
            raise IncompleteCharacterError(
                f"Character {character_id!r} is incomplete: missing {', '.join(missing)}")
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
