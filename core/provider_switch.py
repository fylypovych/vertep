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
from .engine_config import _ENDPOINT_ENV_BY_ENGINE
from .first_run import config_root

_OVERRIDE_FILE = "provider-overrides.json"
_SWITCHABLE_SLOTS = frozenset({"llm", "tts", "compute", "image", "video", "video_engine"})
#: Env var that holds the endpoint of each external video engine (Issue #122 P8).
_ENDPOINT_ENV_BY_SLOT = {
    ("video_engine", engine): env
    for engine, env in _ENDPOINT_ENV_BY_ENGINE.items()
}


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


#: Non-secret provider env vars that a Worker executor reads from its own
#: process environment (Issue i.0.0.1.17).  Delivered to the Worker through the
#: stock heartbeat response so a switch reaches the process that actually runs
#: the task.  Secrets are never included here: only the bare, non-secret
#: selection/endpoint values travel; credentials stay in the integration secret
#: store and node-local env.
_PROVIDER_EXECUTOR_ENV_ALLOWLIST = frozenset({
    "VERTEP_LLM_PROVIDER",
    "TTS_PROVIDER",
    "TTS_VOICE",
    "TTS_MODEL",
    "TTS_LANGUAGE",
    "TTS_SPEED",
    "TTS_URL",
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "VERTEP_COMPUTE_PROVIDER",
    "COMFYUI_URL",
    "COMFYUI_DISTRIBUTED_URL",
    "VERTEP_VIDEO_ENGINE",
    "MONEY_PRINTER_URL",
    "SHORTGPT_URL",
})


def provider_executor_config() -> dict[str, str]:
    """Non-secret provider selection for the Worker executor (Issue i.0.0.1.17).

    A provider switch writes ``provider-overrides.json`` and applies it to the
    CORE process, but text/voice/compute tasks execute in a separate Worker
    process that never sees the CORE env.  This read model exposes exactly the
    allowlisted, non-secret subset of the persisted overrides so the stock
    heartbeat response can carry it to the node that actually runs the task.
    """
    overrides = load_provider_overrides()
    return {key: overrides[key] for key in sorted(overrides)
            if key in _PROVIDER_EXECUTOR_ENV_ALLOWLIST}


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


def switch_provider(slot: str, backend: str, actor: str = "web",
                    endpoint: str | None = None) -> dict[str, Any]:
    """Switch one provider slot to another backend with verify-and-rollback.

    ``endpoint`` is accepted for the video engine only (Issue #122 P8): an external
    engine cannot be applied without the address of its runtime, and Settings must be
    able to supply it without a shell. It is persisted through the same override file
    and the same apply → verify → rollback flow as the backend itself.
    """
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
    if endpoint is not None:
        _validate_endpoint(slot, requested, endpoint)
    if before.get("backend") == requested and endpoint is None:
        return {"slot": slot, "backend": requested, "changed": False,
                "matrix": provider_matrix()}

    env_name = str(before.get("env") or "")
    if not env_name or " " in env_name or "/" in env_name:
        raise ProviderSwitchError(409, f"Provider slot '{slot}' is not switchable")

    endpoint_env = ""
    endpoint_value = None
    endpoint_before = None
    endpoint_had_override = False
    endpoint_old_override = None
    if endpoint is not None:
        endpoint_env = _ENDPOINT_ENV_BY_SLOT.get((slot, requested), "")
        if not endpoint_env:
            raise ProviderSwitchError(
                422, f"Slot '{slot}' backend '{requested}' has no endpoint setting")
        endpoint_value = endpoint.strip().rstrip("/")
        endpoint_before = os.environ.get(endpoint_env)

    overrides = load_provider_overrides()
    previous_value = os.environ.get(env_name)
    had_override = env_name in overrides
    old_override = overrides.get(env_name)
    endpoint_had_override = endpoint_env in overrides
    endpoint_old_override = overrides.get(endpoint_env)

    overrides[env_name] = requested
    if endpoint_env:
        overrides[endpoint_env] = endpoint_value
    try:
        _write_overrides(overrides, actor=actor, slot=slot)
    except OSError as error:
        raise ProviderSwitchError(500, f"Could not persist the provider switch: {error}") from error
    os.environ[env_name] = requested
    if endpoint_env:
        os.environ[endpoint_env] = endpoint_value
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
        extra_env = ((endpoint_env, endpoint_had_override, endpoint_old_override,
                      endpoint_before),) if endpoint_env else ()
        _rollback(env_name, overrides, had_override, old_override,
                  previous_value, actor=actor, slot=slot, extra_env=extra_env)
        raise error

    return {"slot": slot, "backend": requested, "changed": True,
            "env": env_name, "matrix": provider_matrix(),
            "effective_config": effective_provider_config(slot),
            "executor_config": provider_executor_config(), **effective}


def worker_provider_acks() -> dict[str, dict[str, str]]:
    """Non-secret provider selection each registered node last reported.

    The Worker applies the heartbeat-delivered selection and echoes what its
    executors actually observe.  This is the CORE-side proof that a switch
    reached the process that runs the task, not only CORE's own environment
    (Issue i.0.0.1.70).  Only the allowlisted, non-secret subset is ever stored,
    so no credential can appear here.
    """
    try:
        from .state import store
    except Exception:  # noqa: BLE001 - a missing store means no proof, not a failure
        return {}
    acks: dict[str, dict[str, str]] = {}
    for name, record in list(getattr(store, "workers", {}).items()):
        if not isinstance(record, dict):
            continue
        config = record.get("provider_config")
        if isinstance(config, dict):
            acks[str(record.get("node_name") or name)] = {
                str(key): str(value) for key, value in config.items()}
    return acks


def executor_backend_mismatches(slot: str) -> list[dict[str, str]]:
    """Nodes whose reported executor config disagrees with CORE for ``slot``.

    An empty list means every node that has reported an ack observes the same
    backend CORE believes is effective — or that no node has reported yet, in
    which case there is nothing to contradict.  A non-empty list is a failed
    Worker activation: CORE must not report success for a backend the executor
    never observed (Issue i.0.0.1.70 / #170.2).
    """
    from adapters.providers import provider_matrix

    entry = provider_matrix().get(slot, {})
    env_name = str(entry.get("env") or "")
    if not env_name:
        return []
    expected = str(entry.get("backend") or "").strip().lower()
    mismatches: list[dict[str, str]] = []
    for node, ack in worker_provider_acks().items():
        if env_name not in ack:
            continue
        observed = str(ack[env_name]).strip().lower()
        if observed != expected:
            mismatches.append({"node_name": node, "observed": observed, "expected": expected})
    return mismatches


def effective_provider_config(slot: str | None = None,
                              node_name: str | None = None) -> dict[str, Any]:
    """Effective configuration after a switch and after a restart (Issue i.0.0.1.17).

    Combines the live ``provider_matrix()`` entry with the persisted
    ``provider-overrides.json`` selection, the allowlisted executor subset that
    the Worker is delivered through the heartbeat channel and the subset each
    node reported back as applied.  A caller (Settings read model, preview gate,
    regression test) can prove that the persisted choice, the live CORE matrix,
    the delivered Worker config and the Worker ack agree — a failed activation
    never reports success because any mismatch raises before a success payload is
    produced.
    """
    from adapters.providers import provider_matrix

    matrix = provider_matrix()
    overrides = load_provider_overrides()
    executor = provider_executor_config()
    acks = worker_provider_acks()
    if slot is None:
        return {"matrix": matrix, "overrides": overrides,
                "executor_config": executor, "worker_acks": acks}
    entry = matrix.get(slot, {})
    env_name = str(entry.get("env") or "")
    persisted = overrides.get(env_name) if env_name else None
    effective = {"slot": slot, "backend": entry.get("backend"),
                 "configured": entry.get("configured"),
                 "env": env_name or None, "persisted": persisted,
                 "executor_config": executor,
                 "worker_acks": acks,
                 "matrix": {slot: entry}}
    if persisted is not None and entry.get("backend") != persisted:
        raise ProviderSwitchError(
            409, f"Backend '{entry.get('backend')}' has not reached the executor yet "
            f"(persisted '{persisted}' for slot '{slot}')")
    if node_name and env_name:
        ack = acks.get(str(node_name), {})
        observed = ack.get(env_name)
        if observed is not None and str(observed).strip().lower() != str(entry.get("backend") or "").strip().lower():
            raise ProviderSwitchError(
                409, f"Worker '{node_name}' still observes '{observed}' for slot '{slot}', "
                f"not '{entry.get('backend')}'")
    return effective


def _validate_endpoint(slot: str, requested: str, endpoint: str) -> None:
    """Accept only a bare http(s) origin — no credentials, path, query or fragment.

    Settings may give the operator an address for an engine runtime, but never one that
    carries a secret: an endpoint ends up in the persisted override file and in every
    read model that shows where the runtime lives.
    """
    from urllib.parse import urlsplit

    if not slot == "video_engine":
        raise ProviderSwitchError(422, f"Slot '{slot}' has no endpoint setting")
    value = endpoint.strip()
    if not value:
        raise ProviderSwitchError(422, "endpoint is required")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ProviderSwitchError(422, f"endpoint is not a valid URL: {error}") from error
    if parts.scheme not in {"http", "https"}:
        raise ProviderSwitchError(422, "endpoint must be http or https")
    if not parts.hostname:
        raise ProviderSwitchError(422, "endpoint must contain a host")
    if parts.username or parts.password:
        raise ProviderSwitchError(422, "endpoint must not contain credentials")
    if parts.path.strip("/") or parts.query or parts.fragment:
        raise ProviderSwitchError(
            422, "endpoint must be a bare origin without path, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderSwitchError(422, "endpoint port is out of range")
    if requested == "native":
        raise ProviderSwitchError(422, "Backend 'native' has no endpoint setting")


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
              actor: str, slot: str,
              extra_env: tuple = ()) -> None:
    if had_override and old_override is not None:
        overrides[env_name] = old_override
    else:
        overrides.pop(env_name, None)
    for name, had_extra, old_extra, previous_extra in extra_env:
        if not name:
            continue
        if had_extra and old_extra is not None:
            overrides[name] = old_extra
        else:
            overrides.pop(name, None)
    try:
        _write_overrides(overrides, actor=actor, slot=slot)
    except OSError:
        pass
    if previous_value is None:
        os.environ.pop(env_name, None)
    else:
        os.environ[env_name] = previous_value
    for name, _had_extra, _old_extra, previous_extra in extra_env:
        if not name:
            continue
        if previous_extra is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous_extra
    reset_provider_registry()
