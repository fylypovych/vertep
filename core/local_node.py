"""Enrollment of the Worker that runs on the CORE host itself (owner decision 20.11.2026).

Production CORE requires node mTLS (``NODE_MTLS_REQUIRED``): a node is accepted only
through the proxy with a client certificate signed by the installation node CA. The
Worker next to CORE used to call ``http://core:8080`` with the shared token and was
refused (401) on every heartbeat, so a single-server installation had no node at all.

CORE now hands its own Worker a single-use registration token through the shared
``config`` volume. The Worker enrolls like any node (its key never leaves it), then
talks to CORE through the proxy with its certificate. Nothing is weakened: the token
is single-use, short-lived, readable only by root (0600) and the ``core`` role is
never offered through the registration API.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ENROLLMENT_FILE = "local-worker-enrollment.json"
TOKEN_TTL_SECONDS = 3600


def _mtls_required() -> bool:
    return os.getenv("NODE_MTLS_REQUIRED", "false").lower() == "true"


def local_node_id() -> str | None:
    import re
    value = str(os.getenv("NODE_NAME", "")).strip().lower()
    return value if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", value) else None


def _plan_runs_worker(config: Path) -> bool:
    try:
        plan = json.loads((config / "deployment-plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return plan.get("role") == "core" and "worker" in (plan.get("services") or [])


def ensure_local_worker_enrollment(now: float | None = None) -> dict:
    """Offer the local Worker an enrollment when it has no node identity yet.

    Idempotent: an enrolled (not revoked) node or a still valid offer means no change.
    Returns what happened, for the caller's log.
    """
    from .first_run import config_root
    from .node_registry import create_registration_token, registered_nodes
    config = config_root()
    node_id = local_node_id()
    if not _mtls_required() or not node_id or not _plan_runs_worker(config):
        return {"state": "not_applicable"}
    if any(node.get("node_id") == node_id and not node.get("revoked_at") for node in registered_nodes()):
        return {"state": "enrolled", "node_id": node_id}
    path = config / ENROLLMENT_FILE
    now = time.time() if now is None else now
    try:
        offer = json.loads(path.read_text(encoding="utf-8"))
        current = (offer.get("pin_certificate") is True
                   and offer.get("core_url") == os.getenv("VERTEP_LOCAL_CORE_URL", "https://proxy:8443"))
        # An offer from 0.0.4.23 (public address, no pinning) is replaced at once.
        if current and offer.get("node_id") == node_id and datetime.fromisoformat(offer["expires_at"]).timestamp() > now + 60:
            return {"state": "offered", "node_id": node_id}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    certificate = Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/tls/vertep.crt"))
    try:
        core_ca = certificate.read_text(encoding="utf-8")
    except OSError:
        return {"state": "no_core_certificate"}
    token = create_registration_token("core", TOKEN_TTL_SECONDS, local_core=True)
    offer = {"node_id": node_id, "token": token["token"], "expires_at": token["expires_at"],
             # The proxy is the only place that verifies node certificates. The Worker reaches it
             # inside the Docker network: the public address (WEB_DOMAIN, often the host name or a
             # VPN address) is not reachable from a container on every host (20.11.2026 stand).
             # The Worker pins this exact certificate instead of checking the host name.
             "core_url": os.getenv("VERTEP_LOCAL_CORE_URL", "https://proxy:8443"),
             "pin_certificate": True,
             "core_ca": core_ca}
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(offer, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    logger.info("Local Worker enrollment offered", extra={"node_id": node_id})
    return {"state": "offered", "node_id": node_id, "created": True}
