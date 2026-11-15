"""Multi-stage script generation: Structure -> Scenes -> Assembly (AGENTS.md §14).

The agent is transport-agnostic: it receives a ``complete(prompt) -> str``
callable.  The Text Worker passes its own LLM transport (``execute_script``);
``ScriptAgent()`` without arguments falls back to the configured LLM client.

Failures are never masked: an unparseable structure, an invalid scene that
stays invalid after its own regeneration budget, or a backend error abort the
task so CORE applies its regular retry/``max_retries`` policy.

i.0.0.4.74 (#474): the whole multi-stage run shares one task budget
(:class:`ScriptBudget`). Before every LLM request — the structure, each scene
and each scene retry — the ``checkpoint`` stops the run on cancellation, lost
lease or an exhausted budget, so no new request starts after the right to run
is gone. A request already in flight is not interrupted: its transport timeout
is cut to the remaining budget, which is the real upper bound of a stop.
"""

import json
import os
import time
from collections.abc import Callable

from pydantic import ValidationError

from adapters.llm_clients import get_llm_client

from .script_schema import ScriptScene, normalize_script

STRUCTURE_STAGE = "STAGE=structure"
SCENE_STAGE = "STAGE=scene"
MAX_SCENES = 200
# Stand-in scene that lets the Assembly contract check structure metadata alone.
_PLACEHOLDER_SCENE = {"prompt": "structure check", "video_prompt": "structure check", "duration": 1}


class ScriptGenerationError(RuntimeError):
    """A script stage produced no usable result within its limits."""


class ScriptBudgetExceeded(ScriptGenerationError):
    """The task budget ran out: a regular failure, so CORE applies its retries."""


class ScriptStopped(RuntimeError):
    """The executor lost the right to run (cancelled or lease lost): no new request."""


class ScriptBudget:
    """One time budget and ownership check for a whole script task.

    ``seconds`` is the existing task timeout (``task.timeout`` /
    ``OLLAMA_SCRIPT_TIMEOUT``), applied to the run instead of to each request.
    ``stop`` returns a reason (``"cancelled"``, ``"lease lost"``) once the
    executor no longer owns the task, or ``None``. ``clock`` is injectable for
    tests.
    """

    def __init__(self, seconds: float, *, stop: Callable[[], str | None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.seconds = float(seconds)
        self._stop = stop
        self._clock = clock = clock or time.monotonic
        self._deadline = clock() + self.seconds

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def check(self) -> None:
        reason = self._stop() if self._stop is not None else None
        if reason:
            raise ScriptStopped(f"Script generation stopped: {reason}")
        if self.remaining() <= 0:
            raise ScriptBudgetExceeded(
                f"Script task budget of {self.seconds:g}s is exhausted")

    def bound(self, timeout: float | None) -> float:
        """Check, then cut a transport timeout to the remaining budget."""
        self.check()
        remaining = self.remaining()
        return remaining if timeout is None else min(float(timeout), remaining)


def _scene_attempts() -> int:
    try:
        return max(1, int(os.getenv("SCRIPT_SCENE_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


class ScriptAgent:
    def __init__(self, complete: Callable[[str], str] | None = None,
                 *, scene_attempts: int | None = None,
                 checkpoint: Callable[[], None] | None = None) -> None:
        self._complete_fn = complete
        self._scene_attempts = scene_attempts
        self._checkpoint = checkpoint

    def generate_script(self, topic: str, system_prompt: str = "", character: dict | None = None,
                        revision: str | None = None, brief: str | None = None) -> dict:
        if os.getenv("DEMO_MODE", "false").lower() == "true":
            return {"title": topic, "description": f"Коротке відео: {topic}", "hashtags": ["#vertep"],
                    "voiceover": topic, "scenes": [{"prompt": topic, "video_prompt": topic, "duration": 1}]}
        context = {"topic": topic, "system_prompt": system_prompt, "character": character,
                   "revision": revision, "brief": brief}
        structure = self._generate_structure(context)
        scenes = [self._generate_scene(context, structure, number, entry)
                  for number, entry in enumerate(structure["scenes_plan"], 1)]
        return self._assemble(topic, structure, scenes)

    # -- transport -----------------------------------------------------------
    def _complete(self, prompt: str) -> str:
        # Before every request: structure, next scene and scene retry (#474).
        if self._checkpoint is not None:
            self._checkpoint()
        if self._complete_fn is not None:
            return self._complete_fn(prompt)
        return get_llm_client().complete(prompt, format_json=True)

    # -- Structure -----------------------------------------------------------
    def _generate_structure(self, context: dict) -> dict:
        target = float(os.getenv("SCRIPT_TARGET_DURATION", "60"))
        prompt = self._build_prompt(context, STRUCTURE_STAGE, f"""
Згенеруй структуру сценарію короткого українськомовного відео на тему: {context['topic']}
Сумарна тривалість має бути близькою до {target:g} секунд.
Поверни лише JSON object без Markdown з полями:
- title: короткий заголовок
- description: опис для соцмереж
- hashtags: масив хештегів
- voiceover: загальний текст озвучки (якщо є)
- scenes_plan: масив планів сцен, кожен з полями: summary, estimated_duration
""")
        data = self._parse_object(self._complete(prompt), "structure")
        plan = data.get("scenes_plan")
        if not isinstance(plan, list) or not plan:
            raise ScriptGenerationError("Script structure has no scenes_plan")
        if len(plan) > MAX_SCENES:
            raise ScriptGenerationError(f"Script structure has more than {MAX_SCENES} scenes")
        entries = []
        for number, entry in enumerate(plan, 1):
            summary = entry.get("summary") if isinstance(entry, dict) else entry
            if not isinstance(summary, str) or not summary.strip():
                raise ScriptGenerationError(f"Script structure scene {number} has no summary")
            duration = entry.get("estimated_duration", 5) if isinstance(entry, dict) else 5
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                duration = 5.0
            entries.append({"summary": summary.strip(),
                            "estimated_duration": duration if 0 < duration <= 600 else 5.0})
        structure = {
            "title": str(data.get("title") or context["topic"]),
            "description": str(data.get("description") or ""),
            "hashtags": data.get("hashtags") or [],
            "voiceover": str(data.get("voiceover") or ""),
            "scenes_plan": entries,
        }
        if isinstance(data.get("platforms"), dict):
            structure["platforms"] = data["platforms"]
        self._check_structure_metadata(structure, context["topic"])
        return structure

    @staticmethod
    def _check_structure_metadata(structure: dict, topic: str) -> None:
        """Validate title/description/hashtags/voiceover/platforms before any scene.

        i.0.0.4.80: the same ``normalize_script`` / ``ScriptDocument`` contract as
        Assembly, applied to the structure with one placeholder scene, so invalid
        metadata fails before the scenes spend LLM requests. Nothing is relaxed.
        """
        metadata = {key: value for key, value in structure.items() if key != "scenes_plan"}
        try:
            normalize_script({**metadata, "scenes": [_PLACEHOLDER_SCENE]}, topic)
        except ValueError as error:
            raise ScriptGenerationError(f"Script structure metadata is invalid: {error}") from error

    # -- Scenes --------------------------------------------------------------
    def _generate_scene(self, context: dict, structure: dict, number: int, entry: dict) -> dict:
        plan = "\n".join(f"{index}. {item['summary']}"
                         for index, item in enumerate(structure["scenes_plan"], 1))
        prompt = self._build_prompt(context, f"{SCENE_STAGE}\nSCENE_INDEX={number}", f"""
Тема відео: {context['topic']}
Заголовок: {structure['title']}
Загальна озвучка: {structure['voiceover']}
План сцен:
{plan}

Згенеруй детальну сцену {number}: {entry['summary']}
Орієнтовна тривалість: {entry['estimated_duration']:g} секунд.
Поверни лише JSON object без Markdown з полями:
- prompt: детальний image prompt для генерації зображення
- video_prompt: короткий опис для відео-анімації
- voiceover: текст озвучки для цієї сцени
- duration: тривалість в секундах (додатне число, макс 600)
""")
        attempts = self._scene_attempts or _scene_attempts()
        errors = []
        for _ in range(attempts):
            raw = self._complete(prompt)
            try:
                return self._validate_scene(raw, entry)
            except ScriptGenerationError as error:
                errors.append(str(error))
        raise ScriptGenerationError(
            f"Scene {number} stayed invalid after {attempts} attempts: {errors[-1]}")

    @classmethod
    def _validate_scene(cls, raw: str, entry: dict) -> dict:
        data = cls._parse_object(raw, "scene")
        prompt = data.get("prompt") or data.get("image_prompt")
        scene = {
            "prompt": prompt,
            "video_prompt": data.get("video_prompt") or prompt,
            "voiceover": data.get("voiceover") or data.get("text") or "",
            # ``null`` means "no value", like an absent key: the planned duration
            # applies instead of a pointless regeneration (i.0.0.4.80).
            "duration": (entry["estimated_duration"] if data.get("duration") is None
                         else data["duration"]),
        }
        try:
            return ScriptScene.model_validate(scene).model_dump()
        except ValidationError as error:
            raise ScriptGenerationError(f"invalid scene: {error.errors()[0].get('msg')}") from error

    # -- Assembly ------------------------------------------------------------
    @staticmethod
    def _assemble(topic: str, structure: dict, scenes: list[dict]) -> dict:
        payload = {key: value for key, value in structure.items() if key != "scenes_plan"}
        payload["scenes"] = scenes
        return normalize_script(payload, topic)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _parse_object(raw: str, stage: str) -> dict:
        if not isinstance(raw, str) or not raw.strip():
            raise ScriptGenerationError(f"LLM returned empty {stage} response")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ScriptGenerationError(f"LLM returned invalid {stage} JSON: {error.msg}") from error
        if not isinstance(data, dict):
            raise ScriptGenerationError(f"LLM {stage} response must be a JSON object")
        return data

    @staticmethod
    def _build_prompt(context: dict, stage: str, user_prompt: str) -> str:
        parts = [stage]
        if context.get("system_prompt"):
            parts.append(context["system_prompt"].strip())
        character = context.get("character")
        if character:
            style = character.get("visual", {}).get("style") or character.get("style")
            if style:
                parts.append(f"Візуальний стиль: {style}")
            aspect = character.get("visual", {}).get("aspect_ratio")
            if aspect:
                parts.append(f"Співвідношення сторін: {aspect}")
            generation = character.get("generation", {})
            if generation.get("workflow"):
                parts.append(f"Workflow: {generation['workflow']}")
        if context.get("brief"):
            parts.append(f"Завдання: {context['brief'].strip()}")
        parts.append(user_prompt.strip())
        if context.get("revision"):
            parts.append(f"REVISION INSTRUCTION: {context['revision']}\n"
                         "Застосуй цю інструкцію до сценарію.")
        return "\n\n".join(parts)
