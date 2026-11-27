"""Administrator management of access accounts (owner decision 19.11.2026).

Two access roles exist: ``admin`` and ``moderator``; a moderator works within
the brands listed in its account. Accounts created here live in the persistent
user store (``config/users.json``); accounts from the read-only ``USERS_JSON``
environment and the installation administrator are listed but not changed.
Password hashes are never returned.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{1,63}")
BRAND_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
ROLES = ("admin", "moderator")


def _require_admin(request: Request) -> str:
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        raise HTTPException(403, "Administrator role required")
    return identity[0]


def _installation_admin(username: str) -> bool:
    """The First Run administrator or the environment fallback administrator."""
    from core.security import configured_user
    configured = configured_user()
    if configured and configured[0] == username:
        return True
    return bool(os.getenv("ADMIN_PASSWORD")) and username == os.getenv("ADMIN_USER", "admin")


def _brands(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not BRAND_ID.fullmatch(item) for item in value):
        raise HTTPException(422, "brands must be a list of brand ids")
    from core.brand_store import brand_exists
    unknown = [item for item in value if not brand_exists(item)]
    if unknown:
        raise HTTPException(422, f"Unknown brands: {', '.join(sorted(set(unknown)))}")
    return sorted(set(value))


def _role(value) -> str:
    from core.security import _normalize_role
    role = _normalize_role(value)
    if role not in ROLES:
        raise HTTPException(422, "role must be admin or moderator")
    return role


def _password(value) -> str:
    from core.security import password_policy_violations
    violations = password_policy_violations(str(value or ""))
    if violations:
        raise HTTPException(422, violations[0])
    return str(value)


def _listing() -> list[dict]:
    from core.first_run import user_store
    from core.security import _load_all_users, _normalize_role, configured_user
    stored = user_store()
    configured = configured_user()
    result = []
    for user, record in sorted(_load_all_users().items()):
        if not isinstance(record, dict):
            continue
        role = _normalize_role(record.get("role", "moderator")) or "moderator"
        result.append({"user": user, "role": role,
                       "brands": sorted(record.get("brands") or []) if role == "moderator" else None,
                       "display_name": record.get("display_name"), "email": record.get("email"),
                       "editable": user in stored and not _installation_admin(user)})
    if configured and configured[0] not in {item["user"] for item in result}:
        result.insert(0, {"user": configured[0], "role": "admin", "brands": None,
                          "display_name": configured[1].get("display_name"), "email": configured[1].get("email"),
                          "editable": False})
    return result


@router.get("/api/settings/users")
def list_users(request: Request):
    _require_admin(request)
    return {"users": _listing()}


@router.post("/api/settings/users")
def create_user(payload: dict, request: Request):
    from core.first_run import password_hash, save_user
    from core.security import _load_all_users
    actor = _require_admin(request)
    username = str(payload.get("user", ""))
    if not USERNAME.fullmatch(username):
        raise HTTPException(422, "Invalid username")
    if username in _load_all_users() or _installation_admin(username):
        raise HTTPException(409, "User already exists")
    role = _role(payload.get("role", "moderator"))
    record = {"role": role, "password_hash": password_hash(_password(payload.get("password"))),
              "created_by": actor}
    if role == "moderator":
        record["brands"] = _brands(payload.get("brands"))
    save_user(username, record)
    return next(item for item in _listing() if item["user"] == username)


def _stored(username: str) -> dict:
    from core.first_run import user_store
    if _installation_admin(username):
        raise HTTPException(409, "The installation administrator is changed in its own profile")
    record = user_store().get(username)
    if not isinstance(record, dict):
        raise HTTPException(404, "User not found or not editable here")
    return dict(record)


@router.put("/api/settings/users/{username}")
def update_user(username: str, payload: dict, request: Request):
    from core.first_run import password_hash, save_user
    actor = _require_admin(request)
    record = _stored(username)
    if "role" in payload:
        role = _role(payload["role"])
        if username == actor and role != "admin":
            raise HTTPException(409, "An administrator cannot remove its own administrator role")
        record["role"] = role
    if record.get("role") == "moderator" and "brands" in payload:
        record["brands"] = _brands(payload["brands"])
    if record.get("role") == "admin":
        record.pop("brands", None)
    if payload.get("password"):
        record["password_hash"] = password_hash(_password(payload["password"]))
    save_user(username, record)
    return next(item for item in _listing() if item["user"] == username)


@router.delete("/api/settings/users/{username}")
def delete_user(username: str, request: Request):
    from core.first_run import _write, user_store
    actor = _require_admin(request)
    _stored(username)
    if username == actor:
        raise HTTPException(409, "An administrator cannot delete its own account")
    users = user_store()
    users.pop(username, None)
    _write("users.json", users)
    return {"deleted": username}


@router.get("/api/lab/login")
def lab_login(request: Request, return_to: str = ""):
    """#491 L3: the signed-in administrator enters the LAB panel without a separate
    LAB account; the short-lived assertion goes only to the configured panel."""
    from fastapi.responses import RedirectResponse
    from core.lab_identity import allowed_return, issue
    from core.module.gateway import audit
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        audit("lab.identity", "denied", actor=identity[0] if identity else None, reason="not_admin")
        raise HTTPException(403, "Administrator role required")
    if not allowed_return(return_to):
        audit("lab.identity", "denied", actor=identity[0], reason="return_to")
        raise HTTPException(422, "Unknown LAB panel address")
    audit("lab.identity", "allowed", actor=identity[0])
    return RedirectResponse(f"{return_to}#vertep_identity={issue(identity[0], identity[1])}", status_code=302)


def _admin(request: Request) -> str:
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        raise HTTPException(403, "Administrator role required")
    return identity[0]


@router.post("/api/lab/connection-token")
def lab_connection_token(request: Request):
    """#491: a one-time token (1 h) the LAB bootstrap uses to connect itself to this CORE."""
    from core.lab_connection import issue_token
    from core.module.gateway import audit
    actor = _admin(request)
    result = issue_token()
    audit("lab.connection", "token_issued", actor=actor)
    return result


@router.get("/api/lab/connection")
def lab_connection(request: Request):
    from core.lab_connection import status
    _admin(request)
    return status()


@router.delete("/api/lab/connection")
def lab_disconnect(request: Request):
    """Revokes the LAB's GPU-lease credential; the panel address is cleared too."""
    from core.lab_connection import disconnect
    from core.lab_identity import set_panel_url
    from core.module.gateway import audit
    actor = _admin(request)
    disconnect()
    set_panel_url("")
    audit("lab.connection", "disconnected", actor=actor)
    return {"connected": False}


@router.post("/api/lab/connect")
async def lab_connect(request: Request):
    """Called by the LAB bootstrap with the one-time token (no Vertep session)."""
    from core.lab_connection import LabConnectionError, connect
    from core.module.gateway import audit
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, "Invalid LAB connection request")
    try:
        result = connect(payload.get("token"), str(payload.get("csr") or ""), str(payload.get("panel_url") or ""))
    except PermissionError as error:
        audit("lab.connection", "denied", reason=str(error))
        raise HTTPException(401, str(error)) from error
    except LabConnectionError as error:
        audit("lab.connection", "denied", reason=str(error))
        raise HTTPException(422, str(error)) from error
    audit("lab.connection", "connected", panel=payload.get("panel_url"))
    return result


@router.get("/api/lab/frame")
def lab_frame(request: Request):
    """#491 L2: the LAB panel inside Vertep. The page asks over XHR (same origin, session
    cookie) and puts the answer into its frame: panel address + short-lived assertion."""
    from core.lab_identity import issue, panel_url
    from core.module.gateway import audit
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        audit("lab.identity", "denied", actor=identity[0] if identity else None, reason="not_admin")
        raise HTTPException(403, "Administrator role required")
    url = panel_url()
    if not url:
        return {"url": None}
    audit("lab.identity", "allowed", actor=identity[0], via="frame")
    return {"url": f"{url}/#vertep_identity={issue(identity[0], identity[1])}", "panel": url}


@router.get("/api/lab/panel")
def lab_panel(request: Request):
    """#491 L3: the configured LAB panel address for the administrator's «Лабораторія» entry."""
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        raise HTTPException(403, "Administrator role required")
    from core.lab_identity import panel_url
    return {"url": panel_url() or None}
