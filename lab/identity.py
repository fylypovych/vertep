"""Vertep administrator identity in the LAB (#491 L3).

The LAB accepts the short-lived assertion CORE signs for a signed-in Vertep
administrator. CORE's public key comes over the LAB's authenticated lease
channel (``LAB_RESOURCE_URL`` + ``LAB_RESOURCE_TOKEN``) or from
``LAB_VERTEP_IDENTITY_KEY`` (PEM); the LAB never holds a CORE private key.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUDIENCE = "vertep-lab"
ISSUER = "vertep-core"
_lock = threading.Lock()
_cache: dict = {"key": None}


class IdentityError(ValueError):
    pass


def _fetch_public_key() -> str:
    configured = os.getenv("LAB_VERTEP_IDENTITY_KEY", "").strip()
    if configured:
        return configured
    base = os.getenv("LAB_RESOURCE_URL", "").rstrip("/")
    if not base:
        raise IdentityError("Vertep identity is not configured")
    import httpx
    from .tls import resource_context
    response = httpx.get(f"{base}/identity-key", timeout=10, verify=resource_context(),
                         headers={"x-vertep-lab-token": os.getenv("LAB_RESOURCE_TOKEN", "")})
    response.raise_for_status()
    body = response.json()
    if body.get("algorithm") != "Ed25519" or body.get("audience") != AUDIENCE:
        raise IdentityError("Unexpected Vertep identity key")
    return body["public_key"]


def _public_key() -> Ed25519PublicKey:
    with _lock:
        if _cache["key"] is None:
            key = serialization.load_pem_public_key(_fetch_public_key().encode())
            if not isinstance(key, Ed25519PublicKey):
                raise IdentityError("Vertep identity key is not Ed25519")
            _cache["key"] = key
        return _cache["key"]


def reset_cache() -> None:
    with _lock:
        _cache["key"] = None


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify(token: str, now: float | None = None) -> str:
    """The administrator's login for a valid, unexpired assertion; raises otherwise."""
    try:
        body, signature = token.split(".")
        _public_key().verify(_unb64(signature), body.encode())
        payload = json.loads(_unb64(body))
    except (ValueError, InvalidSignature) as error:
        raise IdentityError("Invalid Vertep identity") from error
    moment = now if now is not None else time.time()
    if payload.get("iss") != ISSUER or payload.get("aud") != AUDIENCE or payload.get("role") != "admin":
        raise IdentityError("Vertep identity is not an administrator for the LAB")
    if not isinstance(payload.get("exp"), int) or moment >= payload["exp"] or moment < payload.get("iat", 0) - 60:
        raise IdentityError("Vertep identity expired")
    user = payload.get("sub")
    if not isinstance(user, str) or not user:
        raise IdentityError("Invalid Vertep identity")
    return user
