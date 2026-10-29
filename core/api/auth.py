from __future__ import annotations

from core.first_run import configured_user
from core.first_run import is_configured
from core.security import _authenticate_user
from core.security import _session_token
from core.security import _valid_session
from core.security import account_password_matches
from core.security import account_profile
from core.security import password_policy
from core.security import password_policy_violations
from core.security import profile_field_violations
from core.security import save_account_profile
from core.security import session_response
from core.security import set_account_password
from core.state import logger
from core.state import request_windows
from core.state import setup_request_windows
from core.system_state import get_system_state
from core.system_state import operation_allowed
from datetime import datetime
from datetime import timezone
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import base64
import base64 as basic64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from fastapi import APIRouter
router = APIRouter()

class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        setup_route = (request.url.path == "/setup" or
                       request.url.path.startswith(("/api/setup", "/setup.html", "/v1/setup.html", "/api/health")))
        if not is_configured() and not setup_route:
            if request.url.path == "/":
                return Response(status_code=307, headers={"Location": "/v1/setup.html"})
            return Response("Complete the First Run Wizard", 503,
                            {"Retry-After": "30", "X-Vertep-Setup": "required"})
        if request.method == "GET" and is_configured() and request.url.path in ("/", "/index.html"):
            # Design choice: v2 (default) vs v1 (classic). Choice is persisted in the cookie.
            if request.cookies.get("vertep_ui") == "v1":
                return Response(status_code=307, headers={"Location": "/v1/"})
        if not is_configured() and setup_route:
            expected_setup = os.getenv("SETUP_TOKEN_HASH", "")
            if expected_setup and request.url.path.startswith("/api/setup"):
                client = request.client.host if request.client else "unknown"
                window = setup_request_windows[client]
                timestamp = time.time()
                while window and window[0] < timestamp - 600:
                    window.popleft()
                expires = os.getenv("SETUP_TOKEN_EXPIRES_AT", "")
                try:
                    expired = bool(expires) and datetime.fromisoformat(
                        expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
                except ValueError:
                    return Response("Setup token expiry configuration is invalid", 503)
                if expired:
                    return Response("Setup code has expired", 410)
                valid_setup_code = secrets.compare_digest(
                    hashlib.sha256(request.headers.get("x-vertep-setup-token", "").encode()).hexdigest(),
                    expected_setup)
                if not valid_setup_code:
                    if len(window) >= int(os.getenv("SETUP_RATE_LIMIT_PER_10_MINUTES", "10")):
                        return Response("Setup attempts are temporarily locked", 429, {"Retry-After": "600"})
                    window.append(timestamp)
                    return Response("Invalid setup code", 401)
            return self._secure(await call_next(request))
        if request.method != "GET":
            path = request.url.path
            operating_state = get_system_state()
            if (str(operating_state.get("operation_id") or "").startswith("restore:")
                    and operating_state.get("state") in {"MAINTENANCE", "RECOVERING"}
                    and path not in {"/api/system/state", "/api/session"}
                    and not (path.startswith("/api/system/backups/") and "/restore" in path)):
                return self._secure(Response("Restore is in progress; writes are blocked", 423))
            operation = None
            if path == "/api/jobs" and request.method == "POST":
                operation = "create_job"
            elif path.startswith("/api/jobs"):
                operation = "mutate_job"
            elif (path.startswith("/api/nodes") and path != "/api/nodes/register"
                  and not re.fullmatch(r"/api/nodes/[a-z0-9-]+/renew", path)):
                operation = "node_control"
            elif path.startswith("/api/settings"):
                operation = "configuration"
            elif path.startswith("/api/system/roles"):
                operation = "configuration"
            elif path.startswith("/api/system/update"):
                operation = "update"
            elif path.startswith("/api/system/recovery"):
                operation = "recovery"
            elif path.startswith("/api/system/restart"):
                operation = "restart"
            elif path.startswith("/api/system/test"):
                operation = "test"
            elif path.startswith("/api/system/backups/") and "/restore" in path and request.method == "POST":
                operation = "restore"
            elif path.startswith("/api/system/backups") and request.method == "POST":
                operation = "backup"
            elif path.startswith("/api/system/models") and request.method in {"POST", "DELETE"}:
                operation = "model_pull"
            elif path.startswith("/api/models/text") and request.method in {"POST", "DELETE"}:
                operation = "model_pull"
            elif path.startswith("/api/characters"):
                operation = "resource_management"
            elif path.startswith("/api/brands"):
                operation = "resource_management"
            elif path.startswith("/api/workflows"):
                operation = "resource_management"
            elif path.startswith("/api/channels"):
                operation = "resource_management"
            if operation and not operation_allowed(operation):
                return Response(f"Operation {operation} is blocked by system state", 423)
        client = request.client.host if request.client else "unknown"
        window = request_windows[client]
        now = time.time()
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= int(os.getenv("RATE_LIMIT_PER_MINUTE", "300")):
            return Response("Rate limit exceeded", 429)
        window.append(now)
        if int(request.headers.get("content-length", "0") or 0) > int(os.getenv("MAX_REQUEST_BYTES", "30000000")):
            return Response("Request too large", 413)
        password = os.getenv("ADMIN_PASSWORD", "")
        machine = ("/api/workers/heartbeat", "/api/tasks/claim", "/api/tasks/renew",
                   "/api/tasks/cancellations", "/api/tasks/result", "/api/logs/ingest",
                   "/api/node/status")
        worker_input = request.method == "GET" and re.fullmatch(r"/api/tasks/[^/]+/inputs/.+", request.url.path)
        if worker_input or request.url.path.startswith(machine) or re.fullmatch(r"/api/nodes/[a-z0-9-]+/renew", request.url.path):
            # Machine routes validate and bind the token to node_name themselves.
            # Keeping that check in one place also supports hashed per-worker tokens.
            response = await call_next(request)
            return self._secure(response)
        internal_key = os.getenv("INTERNAL_API_KEY", "")
        internal_update_routes = {"/api/status", "/api/system/update/check", "/api/system/update/run",
                                  "/api/system/update/readiness", "/api/system/recovery/normal",
                                  "/api/system/restart", "/api/system/test",
                                  "/api/system/backups", "/api/system/state"}
        internal_actions = request.url.path.startswith("/api/nodes/") and request.url.path.endswith("/actions")
        # The Fleet self-calls (Telegram restart/system handlers) read the node
        # listing and the node detail with the internal key; without that the
        # readiness poll could never observe the acknowledged restart.
        internal_node_read = (request.method == "GET"
                              and (request.url.path == "/api/nodes"
                                   or request.url.path.startswith("/api/nodes/")))
        internal_backup_restore = bool(re.fullmatch(
            r"/api/system/backups/[A-Za-z0-9_-]+/restore(?:/confirm|/progress)?", request.url.path))
        if ((request.url.path in internal_update_routes or internal_actions
                or internal_node_read or internal_backup_restore)
                and internal_key
                and secrets.compare_digest(request.headers.get("x-vertep-internal-key", ""), internal_key)):
            response = await call_next(request)
            return self._secure(response)
        public = ("/api/health", "/api/telegram/webhook", "/api/nodes/register")
        setup_routes = ("/api/setup",)
        if request.url.path.startswith(public) or request.url.path.startswith(setup_routes) or request.url.path == "/api/nodes/register":
            response = await call_next(request)
            return self._secure(response)
        demo_mode = os.getenv("DEMO_MODE", "true").lower() == "true"
        demo_paths = {"/api/health", "/api/setup", "/api/setup/health"}
        if (
            demo_mode
            and not os.getenv("ADMIN_PASSWORD", "").strip()
            and not os.getenv("CONFIG_ROOT", "").strip()
            and request.url.path in demo_paths
        ):
            response = await call_next(request)
            return self._secure(response)
        if not configured_user() and not password and not os.getenv("USERS_JSON", "").strip(" {}"):
            # An installation that has not completed setup still answers the
            # session probe: the login page needs ``authenticated: false`` with a
            # 200, never a 401 that would trigger a native browser dialog (§75 S1).
            if request.method == "GET" and request.url.path == "/api/session":
                response = await call_next(request)
                return self._secure(response)
            return self._secure(Response("Authentication required", 401))
        if request.method in {"GET", "HEAD"} and not request.url.path.startswith("/api/"):
            # Issue #75 S1: the Web UI is a static bundle. An unauthenticated
            # visitor must be able to load /login, otherwise the guard redirect
            # has nothing to render. No Vertep data is served outside /api,
            # which stays behind the authentication checks below.
            response = await call_next(request)
            return self._secure(response)
        expected_user = os.getenv("ADMIN_USER", "admin")
        header = request.headers.get("authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            user, supplied = basic64.b64decode(encoded).decode().split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            scheme, user, supplied = "", "", ""
        session_identity = _valid_session(request.cookies.get("vertep_session", ""))
        basic_role = _authenticate_user(user, supplied) if scheme.lower() == "basic" else None
        if not session_identity and not basic_role:
            # The Angular login form submits credentials explicitly. A Basic
            # challenge on its session probe opens a second, native browser login.
            return self._secure(Response("Authentication required", 401))
        actor, role = session_identity or (user, basic_role)
        if request.method != "GET" and role == "viewer" and not request.url.path.startswith("/api/session"):
            return Response("Insufficient role", 403)
        if request.method in {"PUT", "DELETE"} and request.url.path.startswith(("/api/characters", "/api/brands", "/api/workflows")) and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith(("/api/system/update", "/api/system/roles", "/api/system/recovery", "/api/system/restart", "/api/system/test", "/api/system/models")) and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith("/api/system/backups") and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith("/api/settings") and role != "admin":
            return Response("Administrator role required", 403)
        if (request.method != "GET" and request.url.path.startswith("/api/nodes")
                and request.url.path != "/api/nodes/register" and role != "admin"):
            return Response("Administrator role required", 403)
        if session_identity and not basic_role and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/api/session":
            csrf = request.cookies.get("vertep_csrf", "")
            if not csrf or not secrets.compare_digest(csrf, request.headers.get("x-csrf-token", "")):
                return Response("Invalid CSRF token", 403)
        response = await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            logger.info("Administrative action", extra={"action": f"{request.method} {request.url.path}",
                                                         "actor": actor})
        return self._secure(response)

    @staticmethod
    def _secure(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:"
        return response


def _json_error(detail: str, status: int, **extra) -> Response:
    from core.logging_config import secret_redact
    body = {"detail": secret_redact(detail), **extra}
    return Response(content=json.dumps(body, ensure_ascii=False), status_code=status,
                    media_type="application/json")


@router.delete("/api/session")
def logout(response: Response):
    response.delete_cookie("vertep_session")
    response.delete_cookie("vertep_csrf")
    return {"authenticated": False}


@router.get("/api/session")
def session_info(request: Request):
    # Issue #75 S1: the canonical identity is decided once, on the server.
    return session_response(_valid_session(request.cookies.get("vertep_session", "")))


@router.post("/api/session")
def login(request: Request, response: Response):
    auth = request.headers.get("Authorization", "")
    if not auth:
        return _json_error("Missing Authorization", 401)
    scheme, _, b64 = auth.partition(" ")
    if scheme.lower() != "basic":
        return _json_error("Unsupported authentication scheme", 401)
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
        user, supplied = decoded.split(":", 1)
    except Exception:
        return _json_error("Bad authentication header", 401)
    role = _authenticate_user(user, supplied)
    if role is None:
        return _json_error("Unauthorized", 401)
    token = _session_token(user, role)
    response.set_cookie("vertep_session", token, httponly=True, samesite="Strict", max_age=28800)
    csrf = hmac.new(supplied.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    response.set_cookie("vertep_csrf", csrf, samesite="Strict", max_age=28800)
    return session_response((user, role))


def _session_identity(request: Request) -> Response | tuple[str, str]:
    """Return the validated identity or a ready-to-return 401 response."""
    identity = _valid_session(request.cookies.get("vertep_session", ""))
    profile = account_profile(*identity) if identity else None
    return profile or _json_error("Unauthorized", 401)


@router.put("/api/session/profile")
def update_profile(request: Request, payload: dict):
    """Issue #75 S3: self-service name/email fields for the own account."""
    identity = _session_identity(request)
    if isinstance(identity, Response):
        return identity
    updates = {field: payload.get(field) for field in ("display_name", "email")
               if field in payload}
    if not updates:
        return _json_error("Немає полів для оновлення", 400)
    violations = profile_field_violations(updates)
    if violations:
        return _json_error("Недійсні поля профілю", 422, fields=violations)
    if not save_account_profile(identity["user"], updates):
        return _json_error("User not found", 404)
    return account_profile(identity["user"], identity["role"])


@router.put("/api/session/password")
def change_password(request: Request, payload: dict):
    identity = _session_identity(request)
    if isinstance(identity, Response):
        return identity
    user = identity["user"]
    old_password = payload.get("old_password", "")
    new_password = payload.get("new_password", "")
    if not old_password or not new_password:
        return _json_error("Old and new passwords required", 400)
    violations = password_policy_violations(new_password, old_password)
    if violations:
        return _json_error(violations[0], 400)
    if not account_password_matches(user, old_password):
        # Issue #75 S3: a wrong current password stays a recoverable error —
        # no lockout, no partial write, the current session keeps working.
        return _json_error("Current password is incorrect", 400)
    if not set_account_password(user, new_password):
        return _json_error("User not found", 404)
    return {"ok": True, "message": "Password changed successfully",
            "password_policy": password_policy()}

