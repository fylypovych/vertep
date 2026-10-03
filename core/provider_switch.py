"""Durable provider backend switching (Issue #78).

A backend switch must survive a CORE restart and be verifiable immediately
after it is applied, so the flow is: persist the override → apply it to the
live environment → re-read ``provider_matrix()`` to verify → roll everything
back when verification fails.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_write import atomic_write_json
from .first_run import config_root

_OVERRIDE_FILE = "provider-overrides.json"
_SWITCHABLE_SLOTS = frozenset({"llm", "tts", "compute", "image", "video", "video_engine"})


class ProviderSwitchError(Exception):
    """A provider switch was rejected or could not be verified."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _overrides_path() -> Path:
    return config_root() / _OVERRIDE_FILE


def load_provider_overrides() -> dict[str, str]:
    try:
        raw = json.loads(_overrides_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else raw
    return {str(key): str(value) for key, value in overrides.items()}


def _write_overrides(overrides: dict[str, str], actor: str | None = None,
                     slot: str | None = None) -> None:
    payload = {
        "overrides": dict(overrides),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "slot": slot,
    }
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def apply_provider_overrides() -> dict[str, str]:
    """Apply persisted overrides to the live process environment."""
    applied: dict[str, str] = {}
    for env_name, value in load_provider_overrides().items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            continue
        os.environ[env_name] = value
        applied[env_name] = value
    return applied


def reset_provider_registry() -> None:
    """Drop cached provider instances so factories re-read the environment."""
    from adapters import providers as providers_module

    providers_module._providers = None


def switch_provider(slot: str, backend: str, actor: str = "web") -> dict[str, Any]:
    """Switch one provider slot to another backend with verify-and-rollback."""
    from adapters.providers import provider_matrix

    if slot not in _SWITCHABLE_SLOTS:
        raise ProviderSwitchError(409, f"Provider slot '{slot}' is not switchable")
    requested = str(backend or "").strip().lower()
    if not requested:
        raise ProviderSwitchError(422, "backend is required")

    before = provider_matrix().get(slot, {})
    options = list(before.get("options") or [])
    if requested not in options:
        raise ProviderSwitchError(422, f"Unknown backend '{backend}' for slot '{slot}'")
    if before.get("backend") == requested:
        return {"slot": slot, "backend": requested, "changed": False,
                "matrix": provider_matrix()}

    env_name = str(before.get("env") or "")
    if not env_name or " " in env_name or "/" in env_name:
        raise ProviderSwitchError(409, f"Provider slot '{slot}' is not switchable")

    overrides = load_provider_overrides()
    previous_value = os.environ.get(env_name)
    had_override = env_name in overrides
    old_override = overrides.get(env_name)

    overrides[env_name] = requested
    try:
        _write_overrides(overrides, actor=actor, slot=slot)
    except OSError as error:
        raise ProviderSwitchError(500, f"Could not persist the provider switch: {error}") from error
    os.environ[env_name] = requested
    reset_provider_registry()

    try:
        after = provider_matrix().get(slot, {})
        if after.get("configured") is False:
            raise ProviderSwitchError(
                409, f"Backend '{requested}' is not configured for slot '{slot}'")
        if after.get("backend") != requested:
            raise ProviderSwitchError(
                500, f"Backend '{requested}' did not take effect for slot '{slot}'")
        effective = _verify_effective_engine(slot, requested)
    except ProviderSwitchError as error:
        _rollback(env_name, overrides, had_override, old_override,
                  previous_value, actor=actor, slot=slot)
        raise error

    return {"slot": slot, "backend": requested, "changed": True,
            "env": env_name, "matrix": provider_matrix(), **effective}


def _verify_effective_engine(slot: str, requested: str) -> dict[str, Any]:
    """Issue #122 P7: verify a switch on the actual executor, not only in the matrix.

    For the video engine the selected backend only becomes effective when the real
    runtime proves its pinned snapshot; anything else is rolled back by the caller,
    so a failed apply keeps the previous effective engine.
    """
    if slot != "video_engine":
        return {}
    from .engine_config import effective_engine_config

    config = effective_engine_config(probe=True)
    if config["effective"] != requested or not config["agree"]:
        raise ProviderSwitchError(
            409,
            f"Engine '{requested}' is not effective; "
            f"readiness: {config.get('reason') or 'unknown'}",
        )
    if not config["ready"]:
        raise ProviderSwitchError(
            409,
            f"Engine '{requested}' runtime is not ready: "
            f"{config.get('reason') or 'runtime_not_ready'}",
        )
    return {"effective_engine": config}


def _rollback(env_name: str, overrides: dict[str, str], had_override: bool,
              old_override: str | None, previous_value: str | None,
              actor: str, slot: str) -> None:
    if had_override and old_override is not None:
        overrides[env_name] = old_override
    else:
        overrides.pop(env_name, None)
    try:
        _write_overrides(overrides, actor=actor, slot=slot)
    except OSError:
        pass
    if previous_value is None:
        os.environ.pop(env_name, None)
    else:
        os.environ[env_name] = previous_value
    reset_provider_registry()
