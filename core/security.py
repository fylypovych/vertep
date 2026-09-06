"""Authentication and credential-handling helpers for the Vertep CORE.

This is the leaf module of the authentication layer: it deals with password
hashing, session tokens, worker credentials and user authentication. It has no
dependency on ``core.app`` module-level state, so it can be imported from the
API routers and the auth middleware without creating circular imports.
"""
import os
import json
import re
import time
import hmac
import hashlib
import secrets

from .node_registry import verify_node_certificate, verify_node_token
from .first_run import configured_user, session_secret


def _worker_tokens() -> dict[str, str]:
    result = {}
    for item in os.getenv("WORKER_TOKENS", "").split(","):
        if ":" in item:
            node, token = item.split(":", 1)
            result[node.strip()] = token.strip()
    return result


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
        if (not common_names or not secrets.compare_digest(common_names[-1], node_name)
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
    expected = _worker_tokens().get(node_name) or os.getenv("NODE_API_TOKEN", "")
    return not expected or secrets.compare_digest(supplied, expected)


def _valid_worker_request(node_name: str, request) -> bool:
    return _valid_worker_token(node_name, request.headers.get("x-vertep-token", ""),
                               request.headers.get("x-vertep-client-dn", ""),
                               request.headers.get("x-vertep-client-serial", ""))


def _session_token(user: str = "admin", role: str = "admin") -> str:
    expiry = str(int(time.time()) + int(os.getenv("SESSION_TTL", "28800")))
    payload = f"{expiry}:{user}:{role}"
    signature = hmac.new(session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _valid_session(token: str) -> tuple[str, str] | None:
    try:
        payload, signature = token.rsplit(".", 1)
        expiry, user, role = payload.split(":", 2)
        expected = hmac.new(session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        return (user, role) if int(expiry) > time.time() and secrets.compare_digest(signature, expected) else None
    except (ValueError, TypeError):
        return None


def _authenticate_user(user: str, password: str) -> str | None:
    configured = configured_user()
    if configured and secrets.compare_digest(user, configured[0]) and _verify_hash(password, configured[1]["password_hash"]):
        return str(configured[1].get("role", "admin"))
    try:
        users = json.loads(os.getenv("USERS_JSON", "{}"))
    except ValueError:
        users = {}
    record = users.get(user)
    if isinstance(record, dict) and _verify_hash(password, str(record.get("password_hash", ""))):
        return str(record.get("role", "viewer"))
    if secrets.compare_digest(user, os.getenv("ADMIN_USER", "admin")) and secrets.compare_digest(password, os.getenv("ADMIN_PASSWORD", "")):
        return "admin"
    return None
