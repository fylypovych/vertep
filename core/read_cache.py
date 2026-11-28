"""Non-blocking read models and caches for Settings GET routes (Issue #553 AC5).

Ordinary ``GET /api/settings/providers`` and ``GET /api/settings/video-engine``
must return immediately from local configuration and cached state without
synchronous network probes, OAuth refreshes, module gateway HTTP calls, or
repeated Scrypt key derivations. Explicit ``?probe=true`` / ``?refresh=true``
refreshes live state and updates the cache.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from .first_run import config_root

_cache_lock = threading.RLock()

# Cached local-file secret store keyed by file stats & passphrase fingerprint so
# Scrypt key derivation does not run 20+ times on every Settings GET.
_local_secrets_cache: dict[str, Any] = {
    "fingerprint": None,
    "data": None,
}

# Cached publisher platforms snapshot from an explicit probe/refresh.
_publisher_probe_cache: dict[str, Any] = {
    "platforms": None,
    "checked_at": 0.0,
    "env_fingerprint": None,
}

# Cached video-engine runtime probe result from an explicit ?probe=true.
_video_engine_probe_cache: dict[str, Any] = {
    "config_revision": None,
    "endpoint": None,
    "ready": None,
    "reason": None,
    "checked_at": 0.0,
}

# Cached system state when SYSTEM_STATE_BACKEND=postgres so normal GET does not
# open a blocking TCP socket if the caller only needs cached operating state.
_system_state_cache: dict[str, Any] = {
    "value": None,
    "checked_at": 0.0,
}


def _cache_ttl_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("VERTEP_SETTINGS_CACHE_TTL_SECONDS", "60")))
    except ValueError:
        return 60.0


def invalidate_provider_caches() -> None:
    """Drop cached probe and local-secret snapshots after a config/secret write."""
    with _cache_lock:
        _local_secrets_cache.update({"fingerprint": None, "data": None})
        _publisher_probe_cache.update({"platforms": None, "checked_at": 0.0, "env_fingerprint": None})
        _video_engine_probe_cache.update({
            "config_revision": None, "endpoint": None, "ready": None, "reason": None, "checked_at": 0.0,
        })
        _system_state_cache.update({"value": None, "checked_at": 0.0})


def reset_read_caches() -> None:
    """Alias for tests to start from a cold cache."""
    invalidate_provider_caches()


def _stat_tuple(path: Path) -> tuple[bool, int, int]:
    try:
        st = path.stat()
        return (True, int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (False, 0, 0)


def _passphrase_fingerprint() -> str:
    raw = os.getenv("SECRET_STORE_PASSPHRASE", "")
    file_path = os.getenv("SECRET_STORE_PASSPHRASE_FILE", "")
    file_stat = _stat_tuple(Path(file_path)) if file_path else (False, 0, 0)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw else ""
    return f"{digest}|{file_path}|{file_stat}"


def _cached_local_secrets() -> dict[str, Any]:
    """Read local file secret store with mtime+size caching (zero network I/O).

    When ``STORAGE_BACKEND=postgres``, returns ``{}`` instead of opening a
    blocking PostgreSQL connection on an ordinary read.
    """
    if os.getenv("STORAGE_BACKEND", "file").lower() == "postgres":
        return {}
    root = config_root()
    enc_path = root / "secrets.enc.json"
    key_path = root / "secret-store.key"
    enc_stat = _stat_tuple(enc_path)
    if not enc_stat[0]:
        # No encrypted store yet — check legacy plaintext files without creating
        # or sealing a new key on a read-only GET.
        legacy: dict[str, Any] = {}
        from .first_run import _read
        legacy.update(_read("secrets.json"))
        legacy.update(_read("bootstrap-secrets.json"))
        return legacy
    fingerprint = (str(root), enc_stat, _stat_tuple(key_path), _passphrase_fingerprint())
    with _cache_lock:
        if _local_secrets_cache["fingerprint"] == fingerprint and isinstance(_local_secrets_cache["data"], dict):
            return dict(_local_secrets_cache["data"])
    try:
        from .first_run import _read_encrypted_secrets
        data = _read_encrypted_secrets()
    except Exception:  # noqa: BLE001 - unreadable store must not crash read model
        return {}
    if not isinstance(data, dict):
        return {}
    with _cache_lock:
        _local_secrets_cache["fingerprint"] = fingerprint
        _local_secrets_cache["data"] = dict(data)
    return dict(data)


_SECRET_STORE_KEYS = {
    "YOUTUBE_ACCESS_TOKEN": "youtube_access_token",
    "YOUTUBE_REFRESH_TOKEN": "youtube_refresh_token",
    "YOUTUBE_CLIENT_ID": "youtube_client_id",
    "YOUTUBE_CLIENT_SECRET": "youtube_client_secret",
    "FACEBOOK_ACCESS_TOKEN": "facebook_access_token",
    "INSTAGRAM_ACCESS_TOKEN": "instagram_access_token",
    "INSTAGRAM_USER_ID": "instagram_user_id",
    "TIKTOK_ACCESS_TOKEN": "tiktok_access_token",
    "THREADS_ACCESS_TOKEN": "threads_access_token",
}

_IDENTIFIER_ENV = {
    "facebook": "FACEBOOK_PAGE_ID",
    "instagram": "INSTAGRAM_USER_ID",
    "threads": "THREADS_USER_ID",
}


def _secret_value(env_name: str, local_secrets: dict[str, Any]) -> str:
    if not env_name:
        return ""
    value = os.getenv(env_name.upper())
    if value:
        return value
    store_key = _SECRET_STORE_KEYS.get(env_name.upper(), env_name.lower())
    stored = local_secrets.get(store_key)
    return str(stored) if stored else ""


def _publisher_env_fingerprint() -> tuple:
    from adapters.publisher import PUBLISHERS
    items = [
        ("PUBLISHER_MOCK", os.getenv("PUBLISHER_MOCK", "")),
        ("DEMO_MODE", os.getenv("DEMO_MODE", "")),
        ("STORAGE_BACKEND", os.getenv("STORAGE_BACKEND", "file")),
    ]
    for env_name in sorted(set(_IDENTIFIER_ENV.values())):
        items.append((env_name, bool(os.getenv(env_name))))
    for channel in sorted(PUBLISHERS):
        pub = PUBLISHERS[channel]
        cred_env = getattr(pub, "credential_env", "")
        if cred_env:
            items.append((cred_env, bool(os.getenv(cred_env))))
        provider = pub._token_provider() if hasattr(pub, "_token_provider") else None
        refresh_env = getattr(provider, "refresh_env", "") if provider else ""
        if refresh_env:
            items.append((refresh_env, bool(os.getenv(refresh_env))))
    return tuple(items)


def _local_publisher_platforms(*, probe: bool = False) -> dict[str, dict[str, Any]]:
    """Publisher readiness matrix without synchronous OAuth or module HTTP calls."""
    from adapters.publisher import PUBLISHERS
    from publishers.base import publisher_mock_enabled

    env_fp = _publisher_env_fingerprint()
    now = time.time()

    if probe:
        platforms: dict[str, dict[str, Any]] = {}
        for channel, pub in PUBLISHERS.items():
            platforms[channel] = {
                "configured": bool(pub.configured()),
                "ready": bool(pub.ready()),
                "required_scopes": list(getattr(pub, "required_scopes", [])),
                "missing_scopes": list(pub.missing_scopes()),
            }
        with _cache_lock:
            _publisher_probe_cache.update({
                "platforms": {k: dict(v) for k, v in platforms.items()},
                "checked_at": now,
                "env_fingerprint": env_fp,
            })
        return platforms

    with _cache_lock:
        cached = _publisher_probe_cache["platforms"]
        cached_at = _publisher_probe_cache["checked_at"]
        cached_fp = _publisher_probe_cache["env_fingerprint"]
    if cached is not None and cached_fp == env_fp and (now - cached_at) <= _cache_ttl_seconds():
        return {k: dict(v) for k, v in cached.items()}

    mock = publisher_mock_enabled()
    local_secrets = _cached_local_secrets()
    oauth_states = local_secrets.get("publisher_oauth")
    if not isinstance(oauth_states, dict):
        oauth_states = {}

    platforms = {}
    for channel, pub in PUBLISHERS.items():
        cred_env = getattr(pub, "credential_env", "")
        required_scopes = list(getattr(pub, "required_scopes", []))
        token_provider = pub._token_provider() if hasattr(pub, "_token_provider") else None
        refresh_env = getattr(token_provider, "refresh_env", "") if token_provider else ""

        cred_val = _secret_value(cred_env, local_secrets)
        refresh_val = _secret_value(refresh_env, local_secrets) if refresh_env else ""
        configured = bool(mock or cred_val or refresh_val)

        granted_scopes: list[str] = []
        scopes_known = False
        if token_provider is not None:
            durable = oauth_states.get(cred_env) if isinstance(oauth_states.get(cred_env), dict) else {}
            if durable and not durable.get("revoked") and float(durable.get("expires_at", 0) or 0) > now:
                granted_scopes = list(durable.get("scopes") or [])
            scopes_known = bool(durable.get("scopes_known"))
        missing_scopes = [s for s in required_scopes if s not in set(granted_scopes)] if required_scopes else []

        if mock:
            ready = True
        elif not configured:
            ready = False
        elif channel in _IDENTIFIER_ENV and not os.getenv(_IDENTIFIER_ENV[channel]):
            ready = False
        elif token_provider is not None:
            durable = oauth_states.get(cred_env) if isinstance(oauth_states.get(cred_env), dict) else {}
            persisted_token = str(local_secrets.get(cred_env.lower(), "") or "")
            if durable.get("revoked"):
                ready = False
            elif persisted_token and float(durable.get("expires_at", 0) or 0) > now:
                ready = (not missing_scopes) if (granted_scopes or scopes_known) else True
            elif durable and float(durable.get("expires_at", 0) or 0) <= now:
                # Expired OAuth token needs refresh — never block GET with HTTP refresh.
                ready = False
            elif cred_val:
                ready = (not missing_scopes) if (granted_scopes or scopes_known) else True
            else:
                # Only refresh_token is present without an access token; do not
                # trigger synchronous OAuth token refresh on ordinary GET.
                ready = False
        elif channel == "telegram":
            # Never call the Brands application module over HTTP in ordinary GET.
            ready = _local_telegram_has_targets()
        else:
            ready = True

        platforms[channel] = {
            "configured": configured,
            "ready": ready,
            "required_scopes": required_scopes,
            "missing_scopes": missing_scopes,
        }
    return platforms


def _local_telegram_has_targets() -> bool:
    if os.getenv("STORAGE_BACKEND", "file").lower() == "postgres":
        return False
    try:
        from .brand_store import module_active
        if module_active():
            # Brands module is an external HTTP container — do not probe on GET.
            return False
        from .state import store
        channels = store.repository.list_channels()
        return any(ch.enabled and ch.channel_type == "telegram" and ch.target for ch in channels)
    except Exception:  # noqa: BLE001
        return False


def _tts_slot_local() -> dict[str, Any]:
    """Read the TTS slot from ``adapters.providers`` without network I/O."""
    import adapters.providers as providers_mod
    from adapters.providers import tts_backends

    tts_name = os.getenv("TTS_PROVIDER", "none").lower().strip()
    provider = providers_mod._make_tts(tts_name)
    tts_backend = getattr(provider, "provider", tts_name) or tts_name
    tts_configured = bool(provider.configured())

    explicit_options = getattr(providers_mod, "TTS_OPTIONS", None)
    if isinstance(explicit_options, (list, tuple)):
        options = list(explicit_options)
    else:
        options = ["none", "mock", "piper", "kokoro"]
        has_espeak = (
            any(hasattr(tts_backends, attr) for attr in ("EspeakProvider", "EspeakNGProvider", "EspeakTTSProvider"))
            or not isinstance(providers_mod._make_tts("espeak-ng"), providers_mod.DefaultTTSProvider)
        )
        if has_espeak and "espeak-ng" not in options:
            options.append("espeak-ng")

    return {
        "backend": tts_backend,
        "options": options,
        "env": "TTS_PROVIDER",
        "configured": tts_configured,
    }


def nonblocking_provider_matrix(*, probe: bool = False) -> dict[str, dict[str, Any]]:
    """Build the provider backend matrix without blocking network probes.

    When ``probe=True``, delegates to ``adapters.providers.provider_matrix()``
    once and refreshes the publisher cache. When ``probe=False``, evaluates all
    slots locally with zero network I/O and no repeated Scrypt derivations.
    """
    if probe:
        from adapters.providers import provider_matrix
        matrix = provider_matrix()
        platforms = matrix.get("publisher", {}).get("platforms")
        if isinstance(platforms, dict):
            with _cache_lock:
                _publisher_probe_cache.update({
                    "platforms": {k: dict(v) for k, v in platforms.items()},
                    "checked_at": time.time(),
                    "env_fingerprint": _publisher_env_fingerprint(),
                })
        return matrix

    from adapters.llm_clients import backend_readiness
    from adapters.providers.compute_backends import ComfyUIDistributedProvider
    from adapters.providers.lipsync_backends import MuseTalkProvider
    from adapters.providers.stt_backends import FasterWhisperProvider

    matrix: dict[str, dict[str, Any]] = {}

    # LLM
    llm_name, llm_configured = backend_readiness()
    matrix["llm"] = {
        "backend": llm_name,
        "options": ["ollama", "openai"],
        "env": "VERTEP_LLM_PROVIDER",
        "configured": llm_configured,
    }

    # TTS
    matrix["tts"] = _tts_slot_local()

    # STT
    stt_name = os.getenv("STT_PROVIDER", "faster-whisper").lower().strip() or "faster-whisper"
    matrix["stt"] = {
        "backend": stt_name,
        "options": ["faster-whisper"],
        "env": "STT_URL",
        "configured": FasterWhisperProvider().configured() if stt_name == "faster-whisper" else False,
    }

    # Lip-sync
    lipsync_name = os.getenv("LIPSYNC_PROVIDER", "musetalk").lower().strip() or "musetalk"
    matrix["lipsync"] = {
        "backend": lipsync_name,
        "options": ["musetalk"],
        "env": "MUSETALK_URL",
        "configured": MuseTalkProvider().configured() if lipsync_name == "musetalk" else False,
    }

    # Compute / Image / Video
    compute_name = os.getenv("VERTEP_COMPUTE_PROVIDER", "vertep-worker").lower().strip()
    if compute_name == "comfyui-distributed":
        dist_configured = ComfyUIDistributedProvider().configured()
        compute_active = "comfyui-distributed" if dist_configured else "vertep-worker"
        compute_configured = dist_configured
    else:
        compute_active, compute_configured = "vertep-worker", True
    compute_slot = {
        "backend": compute_active,
        "options": ["vertep-worker", "comfyui-distributed"],
        "env": "VERTEP_COMPUTE_PROVIDER",
        "configured": compute_configured,
    }
    matrix["compute"] = compute_slot
    matrix["image"] = dict(compute_slot)
    matrix["video"] = dict(compute_slot)

    # Assembly
    matrix["assembly"] = {
        "backend": "ffmpeg",
        "options": ["ffmpeg"],
        "env": "—",
        "configured": True,
    }

    # Video engine (no secret-store decryption needed to check configured URL)
    engine_name = os.getenv("VERTEP_VIDEO_ENGINE", "native").lower().strip()
    if engine_name == "money-printer":
        engine_active = "money-printer"
        engine_configured = bool(os.getenv("MONEY_PRINTER_URL", "").strip())
    elif engine_name not in {"", "native"}:
        engine_active, engine_configured = engine_name, False
    else:
        engine_active, engine_configured = "native", True
    matrix["video_engine"] = {
        "backend": engine_active,
        "selected": engine_active,
        "options": ["native", "money-printer"],
        "env": "VERTEP_VIDEO_ENGINE",
        "configured": engine_configured,
    }

    # Publisher
    publisher_options = _local_publisher_platforms(probe=False)
    matrix["publisher"] = {
        "backend": "vertep-official",
        "configured": any(bool(entry.get("configured")) for entry in publisher_options.values()),
        "options": list(publisher_options),
        "env": "PUBLISHER_MOCK / platform credentials",
        "platforms": publisher_options,
    }

    return matrix


def slot_mismatches(
    raw_matrix: dict[str, dict[str, Any]],
    slot: str,
    acks: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    """Compute executor backend mismatches for ``slot`` without recomputing ``provider_matrix()``."""
    entry = raw_matrix.get(slot, {})
    env_name = str(entry.get("env") or "")
    if not env_name:
        return []
    expected = str(entry.get("backend") or "").strip().lower()
    mismatches: list[dict[str, str]] = []
    for node, ack in acks.items():
        if env_name not in ack:
            continue
        observed = str(ack[env_name]).strip().lower()
        if observed != expected:
            mismatches.append({"node_name": node, "observed": observed, "expected": expected})
    return mismatches


def _nonblocking_system_state(*, probe: bool = False) -> dict[str, Any]:
    """Read system operating state without opening a blocking Postgres socket on ordinary GET."""
    from .system_state import SystemState, _path, get_system_state

    if probe or os.getenv("SYSTEM_STATE_BACKEND", "file").lower() != "postgres":
        state = get_system_state()
        with _cache_lock:
            _system_state_cache.update({"value": dict(state), "checked_at": time.time()})
        return state

    now = time.time()
    with _cache_lock:
        cached = _system_state_cache["value"]
        checked_at = _system_state_cache["checked_at"]
    if cached is not None and (now - checked_at) <= _cache_ttl_seconds():
        return dict(cached)

    # Fallback to local mirror file without hitting PostgreSQL over TCP.
    if (config_root() / "removal-in-progress.json").exists():
        return {
            "state": SystemState.MAINTENANCE.value,
            "updated_at": None,
            "reason": "Триває видалення Vertep; dispatch заблоковано",
            "operation_id": "removal",
        }
    import json
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
        return {**value, "state": SystemState(value["state"]).value}
    except FileNotFoundError:
        return {"state": SystemState.NORMAL.value, "updated_at": None, "reason": None, "operation_id": None}
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "state": SystemState.EMERGENCY.value,
            "updated_at": None,
            "reason": "Невідомий або пошкоджений запис стану системи; потрібне відновлення",
            "operation_id": None,
        }


def _nonblocking_required_fields(engine_id: str, local_secrets: dict[str, Any]) -> list[dict[str, Any]]:
    from .engine_config import _ENDPOINT_ENV_BY_ENGINE, _SECRET_ENV_BY_ENGINE, _url_identity

    endpoint_env = _ENDPOINT_ENV_BY_ENGINE.get(engine_id)
    if not endpoint_env:
        return []
    secret_env = _SECRET_ENV_BY_ENGINE.get(engine_id)
    secret_configured = bool(os.getenv(secret_env or "", ""))
    if engine_id == "money-printer" and not secret_configured:
        secret_configured = bool(local_secrets.get("money_printer_token"))
    identity = _url_identity(os.getenv(endpoint_env, ""))
    return [
        {
            "name": "endpoint",
            "env": endpoint_env,
            "value": identity,
            "configured": bool(identity),
            "secret": False,
        },
        {
            "name": "token",
            "env": secret_env,
            "value": None,
            "configured": secret_configured,
            "secret": True,
        },
    ]


def _resolve_video_engine_local(*, probe: bool = False, local_secrets: dict[str, Any]):
    """Resolve the video engine without opening a Postgres secret-store socket on ordinary GET."""
    import adapters.providers as providers_mod
    from adapters.providers.video_engines import MoneyPrinterEngine, NativeVertepEngine, UnsupportedVideoEngine

    if probe or providers_mod._providers is not None or os.getenv("STORAGE_BACKEND", "file").lower() != "postgres":
        if not probe and providers_mod._providers is None:
            # Avoid triggering _create_default_registry() -> MoneyPrinterEngine() -> Scrypt
            # when we already have the cached local secret store.
            engine_name = os.getenv("VERTEP_VIDEO_ENGINE", "native").lower().strip()
            if engine_name == "money-printer":
                env_token = os.getenv("MONEY_PRINTER_TOKEN", "")
                if env_token:
                    return MoneyPrinterEngine(token=env_token)
                stored_token = str(local_secrets.get("money_printer_token") or "")
                engine = MoneyPrinterEngine(token=stored_token)
                engine.secret_source = "secret-store" if stored_token else "unset"
                return engine
            if engine_name not in {"", "native"}:
                return UnsupportedVideoEngine(engine_name)
            return NativeVertepEngine()
        return providers_mod.providers.video_engine()

    engine_name = os.getenv("VERTEP_VIDEO_ENGINE", "native").lower().strip()
    if engine_name == "money-printer":
        env_token = os.getenv("MONEY_PRINTER_TOKEN", "")
        engine = MoneyPrinterEngine(token=env_token)
        engine.secret_source = "env" if env_token else "unset"
        return engine
    if engine_name not in {"", "native"}:
        return UnsupportedVideoEngine(engine_name)
    return NativeVertepEngine()


def nonblocking_video_engine_config(*, probe: bool = False) -> dict[str, Any]:
    """Read model for ``GET /api/settings/video-engine`` without redundant matrix or network calls."""
    from .engine_config import (
        ENGINE_LABELS,
        LOCAL_MONEY_PRINTER_URL,
        _engine_id,
        local_runtime_state,
        verify_effective_engine,
    )
    from .provider_switch import worker_provider_acks
    from .system_state import STATE_OPERATION_POLICY, SystemState

    local_secrets = _cached_local_secrets()
    engine = _resolve_video_engine_local(probe=probe, local_secrets=local_secrets)
    engine_id = _engine_id(engine)

    raw_selected = os.getenv("VERTEP_VIDEO_ENGINE", "native").lower().strip()
    selected = raw_selected or "native"

    report = verify_effective_engine(probe=probe, engine=engine)
    if probe and engine_id != "native":
        with _cache_lock:
            _video_engine_probe_cache.update({
                "config_revision": report["config_revision"],
                "endpoint": report["endpoint"],
                "ready": report["ready"],
                "reason": report["reason"],
                "checked_at": time.time(),
            })

    options = [{"id": opt, "label": ENGINE_LABELS.get(opt, opt)} for opt in ("native", "money-printer")]
    acks = worker_provider_acks()
    mismatches = slot_mismatches(
        {"video_engine": {"env": "VERTEP_VIDEO_ENGINE", "backend": selected}},
        "video_engine",
        acks,
    )
    sys_state_doc = _nonblocking_system_state(probe=probe)
    sys_state = sys_state_doc["state"]
    change_allowed = "configuration" in STATE_OPERATION_POLICY[SystemState(sys_state)]

    return {
        "selected": selected,
        "effective": engine_id,
        "agree": selected == engine_id,
        "label": ENGINE_LABELS.get(engine_id, engine_id),
        "config_revision": report["config_revision"],
        "endpoint": report["endpoint"],
        "secret": report["secret"],
        "fields": _nonblocking_required_fields(engine_id, local_secrets),
        "required_fields": {
            option["id"]: _nonblocking_required_fields(option["id"], local_secrets) for option in options
        },
        "upstream_reference": report["upstream_reference"],
        "ready": report["ready"],
        "reason": report["reason"],
        "ready_for_tasks": bool(report["ready"]) and not mismatches,
        "activation_failed": bool(mismatches),
        "worker_activation": {"ok": not mismatches, "mismatches": mismatches},
        "probed": bool(probe) and engine_id != "native",
        "options": options,
        "local_runtime": local_runtime_state(),
        "runtime_mode": (
            "local"
            if engine_id == "money-printer"
            and os.getenv("MONEY_PRINTER_URL", "").rstrip("/") == LOCAL_MONEY_PRINTER_URL
            else "external" if engine_id != "native" else None
        ),
        "system_state": sys_state,
        "change_allowed": change_allowed,
        "values_exposed": False,
    }
