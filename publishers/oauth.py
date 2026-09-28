# -*- coding: utf-8 -*-
"""
Shared OAuth token handling for all publisher adapters.
Provides a ``BaseTokenProvider`` with caching, secret‑store fallback and
optional refresh support. Platform‑specific providers subclass it and set
required environment variable names.
"""

import os
import time
from typing import Optional

# Helper to fetch a secret either from the environment or from the
# Vertep secret store (core.first_run.ensure_secret_store).
def _get_secret(name: str) -> Optional[str]:
    """Return the secret identified by ``name``.

    The function first checks the process environment (case‑insensitive) and,
    if absent, attempts to read the value from the secret store.  The secret
    store uses snake_case keys – a mapping is provided for known variables.
    """
    value = os.getenv(name.upper())
    if value:
        return value
    try:
        from core.first_run import ensure_secret_store
        store = ensure_secret_store()
        secret_name = {
            "YOUTUBE_ACCESS_TOKEN": "youtube_access_token",
            "YOUTUBE_REFRESH_TOKEN": "youtube_refresh_token",
            "YOUTUBE_CLIENT_ID": "youtube_client_id",
            "YOUTUBE_CLIENT_SECRET": "youtube_client_secret",
            "FACEBOOK_ACCESS_TOKEN": "facebook_access_token",
            "INSTAGRAM_ACCESS_TOKEN": "instagram_access_token",
            "INSTAGRAM_USER_ID": "instagram_user_id",
            "TIKTOK_ACCESS_TOKEN": "tiktok_access_token",
            "THREADS_ACCESS_TOKEN": "threads_access_token",
        }.get(name.upper(), name.lower())
        return store.get(secret_name)
    except Exception:
        return None


class BaseTokenProvider:
    """Base class for OAuth token handling.

    Sub‑classes set the following class attributes:
        * ``credential_env`` – name of the access‑token variable.
        * ``refresh_env``   – name of the refresh‑token variable (optional).
        * ``client_id_env`` – name of the client‑id variable (optional).
        * ``client_secret_env`` – name of the client secret variable (optional).
        * ``token_url`` – endpoint used for token refresh (optional).
    """

    credential_env: str = ""
    refresh_env: str = ""
    client_id_env: str = ""
    client_secret_env: str = ""
    token_url: str = ""
    revoke_url: str = ""

    def __init__(self, transport=None):
        self._transport = transport
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ---------------------------------------------------------------------
    def access_token(self) -> str:
        """Return a valid access token, refreshing it if needed.
        """
        now = time.time()
        if self._cached_token and now < self._token_expires_at:
            return self._cached_token
        token = _get_secret(self.credential_env) or ""
        if token:
            # Assume a typical lifetime of ~3500 s if we cannot determine
            # an exact expiry.
            self._cached_token = token
            self._token_expires_at = now + 3500
            self._token_scopes: list[str] = []  # expiry unknown – scopes not available
            return token
        # No token – try to refresh (if the provider implements it).
        refreshed = self.try_refresh()
        return refreshed or ""

    def token_scopes(self) -> list[str]:
        """Return the OAuth scopes granted by the current access token.

        Returns an empty list when the token has expired or scopes are not
        available (e.g. static tokens without an introspection endpoint).
        """
        if self._cached_token and time.time() < self._token_expires_at and getattr(self, '_token_scopes', None):
            return list(self._cached_token_scopes)
        return []

    @property
    def _token_scopes(self):
        return getattr(self, '_cached_scopes', [])

    @_token_scopes.setter
    def _token_scopes(self, value):
        self._cached_scopes = value

    def refresh_token(self) -> Optional[str]:
        return _get_secret(self.refresh_env) if self.refresh_env else None

    # ---------------------------------------------------------------------
    def try_refresh(self) -> Optional[str]:
        """Attempt to obtain a fresh access token.
        If ``token_url`` and ``refresh_env`` are defined, perform a standard OAuth
        refresh using ``client_id`` and ``client_secret``. Sub‑classes may override
        for platform‑specific behaviour.
        """
        if not self.token_url:
            return None
        refresh = self.refresh_token()
        if not refresh:
            return None
        client_id = _get_secret(self.client_id_env) or ""
        client_secret = _get_secret(self.client_secret_env) or ""
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        try:
            resp = self._post(self.token_url, data=data, timeout=10)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        new_token = body.get("access_token")
        if new_token:
            self._cached_token = new_token
            expires_in = body.get("expires_in", 3600)
            self._token_expires_at = time.time() + int(expires_in) - 60
        return new_token

    # ---------------------------------------------------------------------
    def revoke(self) -> bool:
        """Revoke the current access token via the OAuth revocation endpoint.

        Returns ``True`` if the token was accepted for revocation, ``False``
        if no refresh_token / revocation endpoint is configured or the request
        failed.  Clearing the locally cached token is always performed.
        """
        self._cached_token = None
        self._token_expires_at = 0.0
        if not getattr(self, 'revoke_url', ''):
            return False
        refresh = self.refresh_token()
        client_id = _get_secret(self.client_id_env) or "" if self.client_id_env else ""
        client_secret = _get_secret(self.client_secret_env) or "" if self.client_secret_env else ""
        data = {
            "token": refresh or "",
            "token_type_hint": "refresh_token" if refresh else "access_token",
        }
        if client_id:
            data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret
        try:
            resp = self._post(self.revoke_url, data=data, timeout=10)
        except Exception:
            return False
        return resp.status_code in (200, 204, 410)

    # ---------------------------------------------------------------------
    def reconnect(self) -> str | None:
        """Invalidate the cached token and attempt to obtain a fresh one.

        Clears the locally cached token, then re-reads from the secret store
        and (if available) performs an OAuth refresh.  Returns the new token
        or ``None`` if reconnection failed.
        """
        self._cached_token = None
        self._token_expires_at = 0.0
        token = _get_secret(self.credential_env) or ""
        if token:
            self._cached_token = token
            self._token_expires_at = time.time() + 3500
            return token
        return self.try_refresh()

    # ---------------------------------------------------------------------
    def _post(self, url: str, data: dict, timeout: int = 10):
        if not self._transport:
            from publishers.transport import HttpTransport
            self._transport = HttpTransport()
        return self._transport.post(url, data=data, timeout=timeout)


# -------------------------------------------------------------------------
# YouTube provider – retained for backward compatibility.
class YouTubeTokenProvider(BaseTokenProvider):
    credential_env = "YOUTUBE_ACCESS_TOKEN"
    refresh_env = "YOUTUBE_REFRESH_TOKEN"
    client_id_env = "YOUTUBE_CLIENT_ID"
    client_secret_env = "YOUTUBE_CLIENT_SECRET"
    token_url = "https://oauth2.googleapis.com/token"
    revoke_url = "https://oauth2.googleapis.com/revoke"

    def try_refresh(self) -> Optional[str]:
        refresh = self.refresh_token()
        if not refresh:
            return None
        client_id = _get_secret(self.client_id_env) or ""
        client_secret = _get_secret(self.client_secret_env) or ""
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        try:
            resp = self._post(self.token_url, data=data, timeout=10)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        new_token = body.get("access_token")
        if new_token:
            self._cached_token = new_token
            expires_in = body.get("expires_in", 3600)
            self._token_expires_at = time.time() + int(expires_in) - 60
        return new_token


# -------------------------------------------------------------------------
# Minimal providers for other platforms – they only expose ``access_token``.
class FacebookTokenProvider(BaseTokenProvider):
    credential_env = "FACEBOOK_ACCESS_TOKEN"
    # No refresh support for the simple flow.


class InstagramTokenProvider(BaseTokenProvider):
    credential_env = "INSTAGRAM_ACCESS_TOKEN"
    refresh_env = "INSTAGRAM_REFRESH_TOKEN"
    client_id_env = "INSTAGRAM_CLIENT_ID"
    client_secret_env = "INSTAGRAM_CLIENT_SECRET"
    token_url = "https://graph.facebook.com/v21.0/oauth/access_token"


class TikTokTokenProvider(BaseTokenProvider):
    credential_env = "TIKTOK_ACCESS_TOKEN"
    refresh_env = "TIKTOK_REFRESH_TOKEN"
    client_id_env = "TIKTOK_CLIENT_ID"
    client_secret_env = "TIKTOK_CLIENT_SECRET"
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"

class ThreadsTokenProvider(BaseTokenProvider):
    credential_env = "THREADS_ACCESS_TOKEN"
    refresh_env = "THREADS_REFRESH_TOKEN"
    client_id_env = "THREADS_CLIENT_ID"
    client_secret_env = "THREADS_CLIENT_SECRET"
    token_url = "https://graph.threads.net/v1.0/oauth/access_token"
