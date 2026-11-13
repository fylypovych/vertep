"""Authentication and credential-handling helpers for the Vertep CORE.

This is the leaf module of the authentication layer: it deals with password
hashing, session tokens, worker credentials and user authentication. It has no
dependency on ``core.app`` module-level state, so it can be imported from the
API routers and the auth middleware without creating circular imports.
"""
import os
import re
import time
import hmac
import hashlib
import secrets
from urllib.parse import quote, unquote

from .node_registry import verify_node_certificate, verify_node_token
from .first_run import configured_user, load_all_users, save_user, session_secret


SESSION_ROLES = ("admin", "viewer")
PROFILE_FIELDS = ("display_name", "email")
PASSWORD_MIN_LENGTH = 12
DISPLAY_NAME_MAX_LENGTH = 80
EMAIL_MAX_LENGTH = 254
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


def _worker_tokens() -> dict[str, str]:
    result = {}
    for item in os.getenv("WORKER_TOKENS", "").split(","):
        if ":" in item:
            node, token = item.split(":", 1)
            result[node.strip()] = token.strip()
    return result


def _constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string comparison that also accepts non-ASCII values.

    ``secrets.compare_digest`` rejects ``str`` operands with characters outside
    ASCII, which turned a Cyrillic login or password into a 500 instead of a
    normal authentication result. Encoding both sides to UTF-8 bytes keeps the
    comparison constant-time and byte-exact.
    """
    return secrets.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def _hash_secret(value: str, salt: str = "vertep", iterations: int = 200_000) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _verify_hash(value: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = _hash_secret(value, salt, int(iterations)).rsplit("$", 1)[1]
        return secrets.compare_digest(actual, expected)
    except ValueError:
        return False


def _valid_worker_token(node_name: str, supplied: str, client_dn: str = "",
                        client_serial: str = "") -> bool:
    if os.getenv("NODE_MTLS_REQUIRED", "false").lower() == "true":
        common_names = re.findall(r"(?:^|[,/])\s*CN=([^,/]+)", client_dn)
        if (not common_names or not _constant_time_equals(common_names[-1], node_name)
                or not verify_node_certificate(node_name, client_serial)):
            return False
    if supplied and verify_node_token(supplied, node_name):
        return True
    if _hash_secret(supplied) in {item.strip() for item in os.getenv("REVOKED_TOKEN_HASHES", "").split(",") if item.strip()}:
        return False
    for item in os.getenv("WORKER_TOKEN_HASHES", "").split(","):
        if ":" in item:
            node, encoded = item.split(":", 1)
            if node.strip() == node_name:
                return _verify_hash(supplied, encoded.strip())
    expected = _worker_tokens().get(node_name)
    if expected is None:
        expected = os.getenv("NODE_API_TOKEN", "") or None
    if expected is None or expected == "":
        return False
    return _constant_time_equals(supplied, expected)


def _valid_worker_request(node_name: str, request) -> bool:
    return _valid_worker_token(node_name, request.headers.get("x-vertep-token", ""),
                               request.headers.get("x-vertep-client-dn", ""),
                               request.headers.get("x-vertep-client-serial", ""))


def _session_token(user: str = "admin", role: str = "admin") -> str:
    secret = session_secret()
    if not secret:
        raise ValueError("Session secret is not configured")
    expiry = str(int(time.time()) + int(os.getenv("SESSION_TTL", "28800")))
    # The token travels in a cookie, so the payload must stay ASCII-safe and must
    # not contain the ':' delimiter: a configured username may be non-ASCII.
    payload = f"{expiry}:{quote(user, safe='')}:{quote(role, safe='')}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


_HASH_DIGEST = re.compile(r"[0-9a-f]{64}")


def _usable_password_hash(encoded) -> bool:
    """Whether a stored hash has the shape ``_verify_hash`` can check (never the value)."""
    if not isinstance(encoded, str):
        return False
    parts = encoded.split("$", 3)
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256" or not parts[2]:
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    return iterations > 0 and bool(_HASH_DIGEST.fullmatch(parts[3]))


def admin_credentials_status() -> dict:
    """State of the administrator credentials, by the sources ``_authenticate_user`` uses.

    In authentication order: the administrator created by First Run (installation),
    an admin account in the user store (``users.json`` / ``USERS_JSON``), the
    ``ADMIN_PASSWORD`` fallback. A source counts only when its password can be
    verified; an empty ``ADMIN_PASSWORD`` alone is not "no password" (#457). Only
    the state and source names are reported — never a password or hash.
    """
    from .first_run import installation

    sources: list[str] = []
    problems: list[str] = []
    try:
        administrator = installation().get("administrator")
    except Exception:  # noqa: BLE001 - an unreadable installation is a problem, not success
        administrator, problems = None, ["first_run_unreadable"]
    if administrator is not None:
        if (isinstance(administrator, dict) and str(administrator.get("username") or "").strip()
                and _usable_password_hash(administrator.get("password_hash"))):
            sources.append("first_run")
        else:
            problems.append("first_run_invalid")
    try:
        users = load_all_users()
    except Exception:  # noqa: BLE001
        users, problems = {}, [*problems, "user_store_unreadable"]
    admins = [record for record in users.values()
              if isinstance(record, dict) and _normalize_role(record.get("role")) == "admin"]
    if any(_usable_password_hash(record.get("password_hash")) for record in admins):
        sources.append("user_store")
    if any(not _usable_password_hash(record.get("password_hash")) for record in admins):
        problems.append("user_store_invalid")
    if os.getenv("ADMIN_PASSWORD", ""):
        sources.append("environment")
    state = "configured" if sources else ("invalid" if problems else "missing")
    return {"state": state, "sources": sources, "problems": problems}


def _account_role(user: str, stored_role: str) -> str:
    """The configured installation administrator always retains the admin role.

    Issue #75 S1: an unknown stored role is never promoted; it degrades to the
    least-privileged role instead of reaching the client as an unknown value.
    """
    configured = configured_user()
    if configured and user == configured[0]:
        return "admin"
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if admin_password and user == admin_user:
        return "admin"
    return _normalize_role(stored_role) or "viewer"



def _valid_session(token: str) -> tuple[str, str] | None:
    try:
        secret = session_secret()
        if not secret:
            return None
        payload, signature = token.rsplit(".", 1)
        expiry, encoded_user, encoded_role = payload.split(":", 2)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if int(expiry) > time.time() and _constant_time_equals(signature, expected):
            user = unquote(encoded_user)
            return user, _account_role(user, unquote(encoded_role))
        return None
    except (ValueError, TypeError):
        return None


def _load_all_users() -> dict:
    return load_all_users()


def _normalize_role(role) -> str | None:
    """Return the role when it is a known one, otherwise ``None``.

    Issue #75 S1: the server never guesses a role. A missing or unknown role is
    reported as such so every client can reject the identity instead of falling
    back to a privileged default.
    """
    value = str(role or "").strip().lower()
    return value if value in SESSION_ROLES else None


def _account_record(user: str) -> tuple[dict, str] | None:
    """Locate the stored account of ``user`` and the store that owns it."""
    configured = configured_user()
    if configured and configured[0] == user:
        return configured[1], "administrator"
    record = _load_all_users().get(user)
    return (record, "users") if isinstance(record, dict) else None


def password_policy() -> dict:
    """Single source of truth for the self-service password policy."""
    return {"min_length": PASSWORD_MIN_LENGTH,
            "require_different_from_current": True}


def password_policy_violations(new_password: str, old_password: str | None = None) -> list[str]:
    """Return every policy violation of a requested new password."""
    violations: list[str] = []
    if not new_password:
        violations.append("Новий пароль обов'язковий")
    elif len(new_password) < PASSWORD_MIN_LENGTH:
        violations.append(f"Пароль має містити щонайменше {PASSWORD_MIN_LENGTH} символів")
    if old_password is not None and new_password == old_password:
        violations.append("Новий пароль має відрізнятися від поточного")
    return violations


def profile_field_violations(payload: dict) -> dict[str, str]:
    """Validate the self-service profile fields, returning field -> reason."""
    violations: dict[str, str] = {}
    if "display_name" in payload:
        value = payload.get("display_name")
        if value is not None and not isinstance(value, str):
            violations["display_name"] = "Ім'я має бути текстом"
        elif isinstance(value, str):
            if len(value.strip()) > DISPLAY_NAME_MAX_LENGTH:
                violations["display_name"] = (
                    f"Ім'я не може бути довшим за {DISPLAY_NAME_MAX_LENGTH} символів")
    if "email" in payload:
        value = payload.get("email")
        if value is not None and not isinstance(value, str):
            violations["email"] = "Email має бути текстом"
        elif isinstance(value, str) and value.strip():
            candidate = value.strip()
            if len(candidate) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.match(candidate):
                violations["email"] = "Невірний формат email"
    return violations


def account_profile(user: str, role: str) -> dict | None:
    """Canonical profile of an authenticated ``user``.

    Returns ``None`` when the identity is incomplete (missing user or a role
    outside :data:`SESSION_ROLES`) so every caller rejects it identically.
    """
    resolved = _normalize_role(role)
    if not user or resolved is None:
        return None
    found = _account_record(user)
    record = found[0] if found else {}
    return {"user": user, "login": user, "role": resolved,
            "display_name": str(record.get("display_name") or "").strip() or None,
            "email": str(record.get("email") or "").strip() or None}


def _persist_account(user: str, record: dict, origin: str) -> None:
    """Write an updated account record back to the store that owns it."""
    if origin == "administrator":
        from .first_run import _write, installation
        stored = installation()
        stored["administrator"] = record
        _write("installation.json", stored)
    else:
        save_user(user, record)


def save_account_profile(user: str, updates: dict) -> dict | None:
    """Persist the self-service profile fields for ``user``."""
    found = _account_record(user)
    if not found:
        return None
    record, origin = found
    updated = dict(record)
    for field in PROFILE_FIELDS:
        if field in updates:
            value = updates[field]
            updated[field] = value.strip() if isinstance(value, str) else value
    _persist_account(user, updated, origin)
    return updated


def account_password_matches(user: str, password: str) -> bool:
    """Verify ``password`` against the stored hash of ``user``."""
    found = _account_record(user)
    if not found:
        return False
    return _verify_hash(password, str(found[0].get("password_hash", "")))


def set_account_password(user: str, password: str) -> bool:
    """Store a new password for ``user``; ``False`` when no record exists."""
    from .first_run import password_hash
    found = _account_record(user)
    if not found:
        return False
    record, origin = found
    updated = dict(record)
    updated["password_hash"] = password_hash(password)
    _persist_account(user, updated, origin)
    return True


def session_response(identity: tuple[str, str] | None) -> dict:
    """Canonical ``GET /api/session`` payload for a session identity.

    Anonymous callers get an explicit ``authenticated: false`` shape; a
    partially valid identity is reported as unauthenticated instead of being
    completed with a guessed role.
    """
    profile = account_profile(*identity) if identity else None
    if not profile:
        return {"authenticated": False, "user": None, "role": None,
                "display_name": None, "email": None, "password_policy": password_policy()}
    return {"authenticated": True, **profile, "password_policy": password_policy()}


def _authenticate_user(user: str, password: str) -> str | None:
    configured = configured_user()
    if configured and _constant_time_equals(user, configured[0]) and _verify_hash(password, configured[1]["password_hash"]):
        return "admin"
    users = _load_all_users()
    record = users.get(user)
    if isinstance(record, dict) and _verify_hash(password, str(record.get("password_hash", ""))):
        return _account_role(user, str(record.get("role", "viewer")))
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if admin_password and _constant_time_equals(user, admin_user) and _constant_time_equals(password, admin_password):
        return "admin"
    return None
