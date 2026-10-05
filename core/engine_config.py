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

from adapters.providers.base import BRIDGE_SCHEMA_VERSION, REASON_SNAPSHOT_MISMATCH
from adapters.providers.video_engines import MoneyPrinterEngine, ShortGPTEngine

#: Env var that holds the token of each external engine. Only the *name* is ever
#: reported: a secret value never leaves the process.
_SECRET_ENV_BY_ENGINE = {
    "money-printer": MoneyPrinterEngine.env_token,
    "shortgpt": ShortGPTEngine.env_token,
}

#: Env var that holds the endpoint of each external engine. Taken from the engine
#: classes themselves, so the read model and the switch can never report or persist an
#: address under a name the runtime does not read.
_ENDPOINT_ENV_BY_ENGINE = {
    "money-printer": MoneyPrinterEngine.env_url,
    "shortgpt": ShortGPTEngine.env_url,
}

#: User-facing names of the engines. The technical values stay the API contract
#: (``native`` / ``money-printer`` / ``shortgpt``, Issue #122 §1), so only the labels are
#: added here — Settings must be able to show "Vertep Native" and "MoneyPrinterTurbo"
#: without the operator having to know the env value.
ENGINE_LABELS = {
    "native": "Vertep Native",
    "money-printer": "MoneyPrinterTurbo",
    "shortgpt": "ShortGPT",
}


def engine_options(matrix: dict | None = None) -> list[dict[str, Any]]:
    """Selectable engines with their user-facing names."""
    from adapters.providers import provider_matrix

    entries = (matrix or provider_matrix()).get("video_engine", {})
    options = list(entries.get("options") or [])
    if not options:
        options = ["native"]
    return [
        {"id": option, "label": ENGINE_LABELS.get(option, option)}
        for option in options
    ]


def required_engine_fields(engine_id: str) -> list[dict[str, Any]]:
    """Non-secret inputs an external engine needs before it can be applied.

    Only references are reported — the env var a value belongs to and whether it is set.
    A secret value has no place in a Settings payload, and an endpoint is reduced to its
    identity (scheme, host, port) for the same reason.
    """
    endpoint_env = _ENDPOINT_ENV_BY_ENGINE.get(engine_id)
    if not endpoint_env:
        return []
    secret_env = _SECRET_ENV_BY_ENGINE.get(engine_id)
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
            "configured": bool(os.getenv(secret_env or "", "")),
            "secret": True,
        },
    ]


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


def _url_identity(url: str) -> str:
    """Reduce a URL to the part that proves *which* runtime is addressed.

    A URL may carry credentials in its userinfo, path, query or fragment, so only
    scheme, host and port survive — no credential can pass through this reduction.
    """
    from urllib.parse import urlsplit, urlunsplit

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


def endpoint_identity(engine) -> str:
    """Non-secret endpoint identity of an engine: scheme, host and port only."""
    return _url_identity(str(getattr(engine, "_url", "") or ""))


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


class EngineSnapshotMismatch(RuntimeError):
    """The dispatched snapshot no longer describes this executor (P5/P7).

    The attempt was decided against one exact engine configuration; a node whose
    effective configuration has drifted since the dispatch must refuse it instead
    of rendering with something else. ``code`` is the stable refusal code
    ``engine_snapshot_mismatch`` recorded on the task.
    """

    def __init__(self, field: str, expected: Any, actual: Any) -> None:
        super().__init__(
            f"snapshot field {field!r} drifted: dispatched {expected!r}, "
            f"this executor has {actual!r}"
        )
        self.code = REASON_SNAPSHOT_MISMATCH
        self.field = field
        self.expected = expected
        self.actual = actual


def engine_snapshot_fields(engine) -> dict[str, Any]:
    """Recompute the verifiable engine part of a §9.15 snapshot on this node.

    Only non-secret facts are recomputed: the engine identity, the bridge contract,
    the configuration revision and — for an external engine — the pinned upstream
    reference plus the endpoint and secret *references*. A secret value never takes
    part, so a Worker can prove it runs the same configuration without CORE ever
    handing out a credential.
    """
    profile = getattr(engine, "contract_profile", None)
    engine_id = _engine_id(engine)
    fields: dict[str, Any] = {
        "engine_id": engine_id,
        "bridge_schema_version": BRIDGE_SCHEMA_VERSION,
        "config_revision": engine_config_revision(engine),
    }
    if engine_id != "native":
        fields["upstream_reference"] = getattr(profile, "upstream_reference", None)
        fields["endpoint_reference"] = endpoint_reference(engine)
        fields["secret_reference"] = secret_reference(engine)
    return fields


def verify_engine_snapshot(engine, snapshot: dict | None, *,
                           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Refuse an attempt whose dispatched snapshot drifted on this executor.

    Issue #122 §5 requires the attempt in flight to keep the configuration it was
    dispatched with, and §9.15 makes that snapshot the contract. Comparing only the
    engine name would let a repointed endpoint, a rotated revision or a different
    pinned upstream render the Job, so every field the snapshot declares is
    recomputed here and compared. ``extra`` adds the per-attempt facts that must
    agree with the snapshot as well (aspect, preset, task type).

    A fact the dispatch left undecided is not compared: the snapshot is the reference,
    so a field it does not pin cannot reject this executor. The same rule governs the
    claim gate (``ENGINE_CLAIM_FIELDS``): the attempt is refused over drift, never over
    something CORE itself never decided.
    """
    snapshot = dict(snapshot or {})
    fields = engine_snapshot_fields(engine)
    for name, value in fields.items():
        if snapshot.get(name) in (None, ""):
            continue
        if snapshot[name] != value:
            raise EngineSnapshotMismatch(name, snapshot[name], value)
    for name, value in (extra or {}).items():
        if name in snapshot and snapshot[name] != value:
            raise EngineSnapshotMismatch(name, snapshot[name], value)
    return fields


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

    ``selected``/``effective``/``config_revision`` are always reported together, so a
    caller can prove they describe one and the same engine. What Settings needs to render
    the engine choice is reported next to them: the user-facing names, the non-secret
    inputs each external engine still needs, and whether the current system state allows
    changing the engine at all (Issue #122 P8).
    """
    from .system_state import get_system_state, operation_allowed

    engine = engine if engine is not None else _engine()
    engine_id = _engine_id(engine)
    from adapters.providers import provider_matrix

    matrix = provider_matrix().get("video_engine", {})
    selected = str(matrix.get("selected") or matrix.get("backend") or "native")
    report = verify_effective_engine(probe=probe, engine=engine)
    options = engine_options()
    return {
        "selected": selected,
        "effective": engine_id,
        "agree": selected == engine_id,
        "label": ENGINE_LABELS.get(engine_id, engine_id),
        "config_revision": report["config_revision"],
        "endpoint": report["endpoint"],
        "secret": report["secret"],
        "fields": required_engine_fields(engine_id),
        # What each engine needs, so the operator can fill in an engine they are about to
        # apply — not only the one that is effective right now (Issue #122 P8).
        "required_fields": {
            option["id"]: required_engine_fields(option["id"]) for option in options
        },
        "upstream_reference": report["upstream_reference"],
        "ready": report["ready"],
        "reason": report["reason"],
        "probed": bool(probe) and engine_id != "native",
        "options": options,
        "system_state": get_system_state()["state"],
        "change_allowed": operation_allowed("configuration"),
        "values_exposed": False,
    }