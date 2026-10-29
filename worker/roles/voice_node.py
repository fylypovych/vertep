"""Voice Node role launcher — паралельне співіснення з Audio Module.

Делегує виконання на Audio Module коли увімкнено feature flag
``VERTEP_AUDIO_MODULE_ENABLED``, інакше — на оригінальний executor з
``worker.role_executor``.  Старий робочий шлях зберігається для rollback.
"""

from __future__ import annotations

import os
from typing import Any

from worker.role_executor import execute_role_task as _original_execute
from modules.audio.audio_module import AudioModule

_audio_module: AudioModule | None = None


def _module_enabled() -> bool:
    return os.getenv("VERTEP_AUDIO_MODULE_ENABLED", "false").lower() in {"true", "1", "yes"}


def _get_module() -> AudioModule:
    global _audio_module
    if _audio_module is None:
        _audio_module = AudioModule(executor=lambda task: _original_execute("voice", task, _migration=True))
    return _audio_module


def execute(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Виконати завдання ролі voice через модуль або оригінальний executor."""
    if _module_enabled():
        return _get_module().execute(task)
    return _original_execute("voice", task)


def cancel(task_id: str) -> bool:
    """Скасувати активне завдання модуля синтезу мовлення."""
    if _module_enabled():
        return _get_module().cancel(task_id)
    return False


def health_check() -> dict[str, Any]:
    """Стан ролі voice (module або оригінальний шлях)."""
    if _module_enabled():
        return _get_module().health_check()
    return {"role": "voice", "status": "READY", "path": "original_executor"}


def resource_query() -> dict[str, Any]:
    """Потреби ресурсів ролі voice."""
    if _module_enabled():
        return _get_module().resource_query()
    return {"role": "voice", "min_vram_mb": 0, "gpu_required": False}
