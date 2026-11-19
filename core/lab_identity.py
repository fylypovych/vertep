"""Shared administrator identity for the LAB panel (#491 L3, #493 AC6).

The administrator signs in to Vertep only. CORE issues a short-lived Ed25519-
signed assertion of that administrator for the LAB audience; the LAB verifies
it with CORE's public key, which it fetches over its authenticated lease
channel. No separate LAB owner account or manual owner token is needed for an
administrator; the LAB's own service credentials and grants stay unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

AUDIENCE = "vertep-lab"
ISSUER = "vertep-core"
LIFETIME_SECONDS = 300


def _key_path() -> Path:
    from .first_run import config_root
    return Path(os.getenv("LAB_IDENTITY_KEY_FILE") or config_root() / "lab-identity.key")


def _private_key() -> Ed25519PrivateKey:
    path = _key_path()
    if path.is_file():
        key = serialization.load_pem_private_key(path.read_bytes(), None)
        if isinstance(key, Ed25519PrivateKey):
            return key
        raise ValueError("LAB identity key is not an Ed25519 key")
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    os.replace(temporary, path)
    return key


def public_key_pem() -> str:
    return _private_key().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def issue(user: str, role: str, now: float | None = None) -> str:
    if role != "admin":
        raise PermissionError("Only an administrator signs in to the LAB")
    issued = int(now if now is not None else time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": user, "role": role, "iat": issued,
               "exp": issued + LIFETIME_SECONDS, "jti": uuid.uuid4().hex}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return f"{body}.{_b64(_private_key().sign(body.encode()))}"


def allowed_return(return_to: str) -> bool:
    """Only the configured LAB panel may receive an assertion (no open redirect)."""
    configured = os.getenv("LAB_PANEL_URL", "").strip().rstrip("/")
    if not configured or not return_to:
        return False
    return return_to.rstrip("/") == configured or return_to.startswith(configured + "/")
