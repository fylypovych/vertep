"""Text Node role launcher — паралельне співіснення з Text Module.

Делегує виконання на Text Module коли увімкнено feature flag
``VERTEP_TEXT_MODULE_ENABLED``, інакше — на оригінальний executor з
``worker.role_executor``.  Старий робочий шлях зберігається для rollback.
"""

from __future__ import annotations

import os
from typing import Any

from worker.role_executor import execute_role_task as _original_execute
from modules.text.text_module import TextModule

_text_module: TextModule | None = None


def _module_enabled() -> bool:
    return os.getenv("VERTEP_TEXT_MODULE_ENABLED", "false").lower() in {"true", "1", "yes"}


def _get_module() -> TextModule:
    global _text_module
    if _text_module is None:
        _text_module = TextModule()
    return _text_module


def execute(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Виконати завдання ролі text через модуль або оригінальний executor."""
    if _module_enabled():
        return _get_module().execute(task)
    return _original_execute("text", task)


def cancel(task_id: str) -> bool:
    """Скасувати активне завдання текстового модуля."""
    if _module_enabled():
        return _get_module().cancel(task_id)
    return False


def health_check() -> dict[str, Any]:
    """Стан ролі text (module або оригінальний шлях)."""
    if _module_enabled():
        return _get_module().health_check()
    return {"role": "text", "status": "READY", "path": "original_executor"}


def resource_query() -> dict[str, Any]:
    """Потреби ресурсів ролі text."""
    if _module_enabled():
        return _get_module().resource_query()
    return {"role": "text", "min_vram_mb": 0, "gpu_required": False}
