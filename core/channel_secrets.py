"""One credential set per publication channel (#502, owner decision 19.11.2026).

A channel (one physical account/target, possibly linked to several brands) has
its own secrets in the platform secret store under ``channel:<id>:<name>``; the
brands only reference the channel. A publish task receives the secrets of its
own channel only. A channel without secrets is not published to: the error says
so. The installation-wide platform credentials are used for an existing channel
only when the administrator explicitly allows it on that channel
(``metadata.use_global_credentials``), and every such use is audited.
"""

from __future__ import annotations

import os

from .first_run import INTAKE_ONLY_SECRET_NAMES, INTEGRATION_SECRET_NAMES


def platform_names(channel_type: str) -> set[str]:
    return {name for name in INTEGRATION_SECRET_NAMES
            if name.startswith(f"{channel_type}_") and name not in INTAKE_ONLY_SECRET_NAMES}


def key(channel_id: str, name: str) -> str:
    return f"channel:{channel_id}:{name}"


def mock_publishing() -> bool:
    """Development/test publisher stand-in; never a production path."""
    return os.getenv("PUBLISHER_MOCK", "false").lower() in {"1", "true", "yes"}


def mode(channel, stored: dict | None = None) -> str:
    """``own`` | ``global`` (explicit administrator choice) | ``missing``."""
    from .first_run import ensure_secret_store
    if channel is None:
        return "missing"
    stored = stored if stored is not None else ensure_secret_store()
    if any(stored.get(key(channel.channel_id, name)) for name in platform_names(channel.channel_type)):
        return "own"
    if (channel.metadata or {}).get("use_global_credentials") is True:
        return "global"
    return "missing"


def task_secrets(channel, names: set[str], stored: dict) -> dict:
    """The secrets a publish task of ``channel`` may read (values by their platform names)."""
    current = mode(channel, stored)
    if current == "own":
        return {name: stored[key(channel.channel_id, name)] for name in names
                if stored.get(key(channel.channel_id, name))}
    if current == "global":
        from .module.gateway import audit
        audit("publish.credentials", "global", channel=channel.channel_id, channel_type=channel.channel_type)
        return {name: stored[name] for name in names if stored.get(name)}
    return {}


def store_names(channel, names: set[str], stored: dict) -> dict[str, str]:
    """Where refreshed tokens of this task's channel are written back."""
    if mode(channel, stored) == "global":
        return {name: name for name in names}
    return {name: key(channel.channel_id, name) for name in names}


def describe(channel) -> dict:
    from .first_run import ensure_secret_store
    stored = ensure_secret_store()
    configured = sorted(name for name in platform_names(channel.channel_type)
                        if stored.get(key(channel.channel_id, name)))
    current = mode(channel, stored)
    result = {"channel_id": channel.channel_id, "mode": current, "configured": configured,
              "use_global_credentials": (channel.metadata or {}).get("use_global_credentials") is True}
    if current == "global":
        result["warning"] = "Канал публікує від глобальних облікових даних платформи; задайте власний токен каналу."
    if current == "missing":
        result["warning"] = "Для каналу не задано облікових даних: публікація в нього заблокована."
    return result


def save(channel, values: dict) -> list[str]:
    from .first_run import _secret_lock, _write_encrypted_secrets, ensure_secret_store
    allowed = platform_names(channel.channel_type)
    if not isinstance(values, dict) or not values or set(values) - allowed \
            or any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError(f"Allowed secrets for {channel.channel_type}: {', '.join(sorted(allowed))}")
    with _secret_lock:
        stored = ensure_secret_store()
        for name, value in values.items():
            stored[key(channel.channel_id, name)] = value.strip()
        _write_encrypted_secrets(stored)
    return sorted(values)
