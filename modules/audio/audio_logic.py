"""Чиста бізнес-логіка Audio Module.

Не залежить від CORE, адаптерів або зовнішніх сервісів.  Лише валідація
завдань синтезу мовлення та підготовка конфігурації голосу для TTS-провайдера.
"""

from __future__ import annotations

from typing import Any


def validate_voice_task(task: dict[str, Any]) -> list[str]:
    """Перевірити завдання синтезу мовлення.  Повертає список помилок."""
    errors: list[str] = []
    text = task.get("topic") or task.get("text") or ""
    if not text or not str(text).strip():
        errors.append("text/topic is required for voice synthesis")
    voice = task.get("voice") or ""
    if not voice:
        errors.append("voice is required for voice synthesis")
    return errors


def normalize_audio_artifact(filename: str, kind: str, data: bytes,
                             config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Збудувати канонічний артефакт аудіо (без base64 — тільки структура)."""
    return {
        "filename": filename,
        "kind": kind,
        "size": len(data),
        "config": config or {},
    }


def resolve_voice_config(task: dict[str, Any]) -> dict[str, Any]:
    """Вирішити ефективну конфігурацію голосу для завдання."""
    return {
        "provider": str(task.get("provider") or "default").strip().lower(),
        "voice": str(task.get("voice") or "default").strip(),
        "language": str(task.get("language") or "uk").strip(),
        "model": str(task.get("model") or "").strip(),
        "speed": int(task.get("speed") or 150),
        "character_id": task.get("character_id"),
        "scene_id": task.get("scene_id"),
    }
