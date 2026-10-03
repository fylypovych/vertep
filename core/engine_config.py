"""Effective video-engine configuration (Issue #122 P7).

P7 requires one coherent contract for the engine that actually renders: the
selected backend, the effective engine instance, its endpoint, the *reference* of
the secret it uses and a configuration revision must always agree — and a failed
apply must leave the previous effective engine in place.

This module does not introduce a second switching mechanism: selection, persistence,
apply and rollback stay in :mod:`core.provider_switch` (Issue #78). It adds the
engine-specific part that switch could not know:

* :func:`effective_engine_config` — the read model of what is effective now,
  without exposing any secret value (only its reference);
* :func:`verify_effective_engine` — a readiness probe of the *actual* executor,
  which is what a switch must verify before it is accepted;
* :func:`engine_config_revision` — a stable digest of the non-secret effective
  configuration, recorded in every assembly attempt snapshot so a Worker can
  detect that its own configuration drifted.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

#: Env var that holds the token of each external engine. Only the *name* is ever
#: reported: a secret value never leaves the process.
_SECRET_ENV_BY_ENGINE = {
    "money-printer": "MONEY_PRINTER_TOKEN",
    "shortgpt": "SHORTGPT_TOKEN",
}

#: Env var that holds the endpoint of each external engine.
_ENDPOINT_ENV_BY_ENGINE = {
    "money-printer": "MONEY_PRINTER_URL",
    "shortgpt": "SHORTGPT_URL",
}


def _engine():
    from adapters.providers import providers

    return providers.video_engine()


def _engine_id(engine) -> str:
    value = getattr(engine, "engine_id", None)
    return value if isinstance(value, str) and value else "native"


def _configured(engine) -> bool:
    """``configured`` of an engine, whether it is a method or a plain attribute."""
    value = getattr(engine, "configured", False)
    if callable(value):
        try:
            value = value()
        except Exception:  # noqa: BLE001 - an engine that cannot answer is unconfigured
            return False
    return bool(value)


def endpoint_identity(engine) -> str:
    """Non-secret endpoint identity of an engine: scheme, host and port only.

    A URL may carry credentials in its userinfo, path, query or fragment, so the
    identity is reduced to the part that proves *which* runtime is addressed and
    nothing else — no credentials can survive this reduction.
    """
    from urllib.parse import urlsplit, urlunsplit

    url = str(getattr(engine, "_url", "") or "")
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    host = parts.hostname
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, "", "", ""))


def secret_reference(engine) -> dict[str, Any]:
    """Reference of the secret an engine uses — never its value (P7)."""
    engine_id = _engine_id(engine)
    env_name = _SECRET_ENV_BY_ENGINE.get(engine_id)
    if not env_name:
        return {"env": None, "configured": False, "source": "none"}
    value = os.getenv(env_name, "")
    return {
        "env": env_name,
        "configured": bool(value),
        "source": "env" if value else "unset",
    }


def endpoint_reference(engine) -> dict[str, Any]:
    """Endpoint of an engine and where it came from.

    ``configured`` here answers only "is an endpoint configured", which is what a
    Worker needs to detect drift. Whether the engine as a whole is usable (which
    also depends on its secret) is reported by :func:`verify_effective_engine`.
    """
    engine_id = _engine_id(engine)
    identity = endpoint_identity(engine)
    return {
        "env": _ENDPOINT_ENV_BY_ENGINE.get(engine_id),
        "endpoint": identity,
        "configured": bool(identity),
    }


def engine_config_revision(engine=None) -> str:
    """Stable digest of the non-secret effective engine configuration.

    Two configurations with the same revision are interchangeable for dispatch: the
    same engine, the same endpoint, the same pinned upstream and the same bridge
    contract. A secret *value* is deliberately not part of it — only whether one is
    configured — so rotating a token does not invalidate running attempts, while
    repointing an endpoint does.

    ``engine`` is the engine the revision must describe: a snapshot has to be able to
    record the revision of the engine it was handed, not of whatever the global
    registry happens to resolve at the moment the revision is computed.
    """
    engine = engine if engine is not None else _engine()
    profile = getattr(engine, "contract_profile", None)
    parts = [
        f"engine={_engine_id(engine)}",
        f"selected={os.getenv('VERTEP_VIDEO_ENGINE', '')}",
        f"endpoint={endpoint_identity(engine)}",
        f"upstream={getattr(profile, 'upstream_reference', '')}",
        f"bridge={getattr(profile, 'bridge_version', '')}",
        f"secret_configured={int(bool(secret_reference(engine)['configured']))}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def verify_effective_engine(*, probe: bool = True, engine=None) -> dict[str, Any]:
    """Verify the effective engine on the actual executor (P7).

    Selection and the provider matrix are not proof: an external engine is only
    effective when its runtime answers with the pinned snapshot it was verified
    against. The probe is therefore part of the verify step of a switch, and its
    failure must roll the selection back to the previous effective engine.
    """
    engine = engine if engine is not None else _engine()
    engine_id = _engine_id(engine)
    report: dict[str, Any] = {
        "engine_id": engine_id,
        "endpoint": endpoint_identity(engine),
        "secret": secret_reference(engine),
        "configured": _configured(engine),
        "ready": True if engine_id == "native" else None,
        "reason": None if engine_id == "native" else "not_probed",
        "upstream_reference": getattr(
            getattr(engine, "contract_profile", None), "upstream_reference", None),
        "config_revision": engine_config_revision(engine),
    }
    if engine_id == "native":
        return report
    if not report["configured"]:
        report["ready"] = False
        report["reason"] = "endpoint_not_configured"
        return report
    if not probe:
        return report
    declare = getattr(engine, "capabilities", None)
    if declare is None:
        report["ready"] = False
        report["reason"] = "readiness_unavailable"
        return report
    try:
        declared = declare()
    except Exception as error:  # noqa: BLE001 - any failure means "not ready"
        report["ready"] = False
        report["reason"] = f"readiness_error:{type(error).__name__}"
        return report
    if not isinstance(declared, dict):
        report["ready"] = False
        report["reason"] = "readiness_unavailable"
        return report
    report["ready"] = bool(declared.get("ready"))
    if not report["ready"]:
        health = declared.get("health") if isinstance(declared.get("health"), dict) else {}
        report["reason"] = str(health.get("reason") or "runtime_not_ready")
    return report


def effective_engine_config(*, probe: bool = False, engine=None) -> dict[str, Any]:
    """Read model of the effective video-engine configuration.

    ``selected``/``effective``/``config_revision`` are always reported together, so
    a caller can prove they describe one and the same engine.
    """
    engine = engine if engine is not None else _engine()
    engine_id = _engine_id(engine)
    from adapters.providers import provider_matrix

    matrix = provider_matrix().get("video_engine", {})
    report = verify_effective_engine(probe=probe, engine=engine)
    return {
        "selected": str(matrix.get("backend") or "native"),
        "effective": engine_id,
        "agree": str(matrix.get("backend") or "native") == engine_id,
        "config_revision": report["config_revision"],
        "endpoint": report["endpoint"],
        "secret": report["secret"],
        "upstream_reference": report["upstream_reference"],
        "ready": report["ready"],
        "reason": report["reason"],
        "options": list(matrix.get("options") or []),
        "values_exposed": False,
    }