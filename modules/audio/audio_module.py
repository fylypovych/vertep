"""Audio Module API — обгортка над чистою бізнес-логікою та TTS-провайдером.

Реалізує Module API: ``execute``, ``cancel``, ``health_check``, ``resource_query``.
Виклики TTS робляться через провайдера, а не напряму з модуля.
"""

from __future__ import annotations

from typing import Any

from modules.audio.audio_logic import (
    normalize_audio_artifact,
    resolve_voice_config,
    validate_voice_task,
)

logger = __import__("logging").getLogger("modules.audio")


class AudioModule:
    """Audio Module: паралельне співіснення з Voice Node через feature flag."""

    def __init__(self, provider: Any | None = None) -> None:
        self.provider = provider
        self._active_tasks: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "audio"

    def health_check(self) -> dict[str, Any]:
        """Повернути стан модуля (без викликів TTS)."""
        return {
            "module": "audio",
            "status": "READY",
            "provider": bool(self.provider),
            "active_tasks": len(self._active_tasks),
        }

    def resource_query(self) -> dict[str, Any]:
        """Потреби ресурсів Audio Module (CPU-only, без GPU)."""
        return {
            "module": "audio",
            "min_vram_mb": 0,
            "cpu_cores": 1,
            "ram_mb": 1024,
            "gpu_required": False,
        }

    def execute(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """Виконати завдання синтезу мовлення через провайдера."""
        errors = validate_voice_task(task)
        if errors:
            raise ValueError(f"Invalid voice task: {'; '.join(errors)}")
        task_id = str(task.get("task_id") or task.get("job_id") or "unknown")
        config = resolve_voice_config(task)
        if self.provider is None:
            raise RuntimeError("Audio Module requires a TTS provider")
        text = str(task.get("topic") or task.get("text"))
        audio = self.provider.synthesize(
            text=text,
            voice=config["voice"],
            speed=config["speed"],
        )
        if not audio:
            raise RuntimeError("TTS provider returned empty audio")
        name = f"speech-{config.get('scene_id') or 'voice'}.wav"
        self._active_tasks[task_id] = {"type": "voice", "voice": config["voice"], "scene_id": config.get("scene_id")}
        return [normalize_audio_artifact(name, "audio", audio, config)]

    def cancel(self, task_id: str) -> bool:
        """Скасувати активне завдання за task_id."""
        if task_id in self._active_tasks:
            del self._active_tasks[task_id]
            return True
        if self.provider is not None and hasattr(self.provider, "cancel"):
            try:
                return self.provider.cancel()
            except Exception:
                return False
        return False
