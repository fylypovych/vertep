"""Base class for live Publisher adapters.

A publisher resolves the backend through configuration (``PUBLISHER_MOCK`` or a
platform credential env var), then either returns a mock receipt or performs the
real API upload through an injectable ``HttpTransport``.
"""

from __future__ import annotations

import os
import time
import re
import uuid
import httpx

from publishers.transport import HttpTransport


def check_response(response) -> None:
    """Raise a descriptive error on a non-2xx response.

    Avoids ``Response.raise_for_status()`` which requires a bound ``request``
    (absent on synthetic responses in tests) and surfaces the platform API error.
    """
    if response.status_code < 400:
        return
    detail = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message", error))
            else:
                detail = str(error or data)
    except Exception:  # noqa: BLE001
        detail = response.text[:200]
    raise RuntimeError(
        f"HTTP {response.status_code} ({response.reason_phrase}): {detail}"
    )


def wait_for_container(transport, url: str, token: str, field: str, *, ready: str = "FINISHED") -> None:
    """Meta publishes a media container only after it finishes processing the video."""
    deadline = time.monotonic() + float(os.getenv("PUBLISHER_CONTAINER_TIMEOUT_SECONDS", "300"))
    while True:
        response = transport.get(url, params={"fields": field, "access_token": token})
        check_response(response)
        status = str(response.json().get(field) or "")
        if status == ready:
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Media container failed: {status}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Media container is not ready: {status}")
        time.sleep(float(os.getenv("PUBLISHER_CONTAINER_POLL_SECONDS", "60")))


def fetch_permalink(transport, url: str, token: str, field: str = "permalink") -> str | None:
    """Read the post link after a successful publish.

    The post already exists here: a failure must cost only the link, because
    reporting FAILED would make the retry publish a duplicate.
    """
    try:
        response = transport.get(url, params={"fields": field, "access_token": token})
        check_response(response)
        return response.json().get(field) or None
    except Exception:  # noqa: BLE001
        return None


def publisher_mock_enabled() -> bool:
    """Mock publication exists only in demo mode: production must never fake a post."""
    return (os.getenv("PUBLISHER_MOCK", "false").lower() == "true"
            and os.getenv("DEMO_MODE", "false").lower() == "true")


def _mock_receipt(channel: str, video_path: str, metadata: dict) -> dict:
    publication_id = f"mock-{uuid.uuid4().hex[:12]}"
    return {
        "channel": channel,
        "status": "PUBLISHED",
        "id": publication_id,
        "url": f"https://example.invalid/{channel}/{publication_id}",
        "upload": {
            "mode": "mock-resumable",
            "bytes": os.path.getsize(video_path) if os.path.isfile(video_path) else 0,
            "parts": 1,
        },
        "metadata": metadata,
    }


class Publisher:
    """Base publisher wiring configuration, mock mode and error handling."""

    channel = "unknown"
    credential_env = ""
    required_scopes: list[str] = []

    def __init__(self, transport=None) -> None:
        self.transport = transport or HttpTransport()

    def configured(self) -> bool:
        mock = publisher_mock_enabled()
        from publishers.oauth import _get_secret
        provider = self._token_provider()
        return mock or bool(_get_secret(self.credential_env)) or bool(provider and provider.refresh_token())

    def _token_provider(self):
        from publishers import oauth
        providers = {"youtube": oauth.YouTubeTokenProvider, "tiktok": oauth.TikTokTokenProvider,
                     "facebook": oauth.FacebookTokenProvider, "instagram": oauth.InstagramTokenProvider,
                     "threads": oauth.ThreadsTokenProvider}
        factory = providers.get(self.channel)
        return factory(self.transport) if factory else None

    def ready(self, metadata: dict | None = None) -> bool:
        """Return True when credentials are present and (if OAuth) a token is available."""
        if publisher_mock_enabled():
            return True
        if not self.configured():
            return False
        metadata = metadata or {}
        if self._missing_identifier(metadata):
            return False
        token_provider = self._token_provider() if hasattr(self, "_token_provider") else None
        if token_provider is None:
            return True
        try:
            if not bool(token_provider.access_token()):
                return False
            # Only enforce scopes when the token provider actually reports them;
            # unknown scopes (empty list) stay backward compatible.
            if self.token_scopes() or token_provider.scopes_known():
                return not self.missing_scopes()
            return True
        except Exception:
            return False

    def _probe_request(self, token: str) -> tuple[str, dict] | None:
        """A read-only API call proving the credential; ``None`` when the channel has none."""
        return None

    def _readiness_token(self) -> str | None:
        provider = self._token_provider()
        return provider.access_token() if provider is not None else None

    def readiness(self) -> dict:
        """Whether this channel can really publish, and why not. Nothing is uploaded."""
        configured = bool(self.configured())

        def state(ready: bool, reason: str | None = None, missing: list[str] | None = None) -> dict:
            return {"ready": ready, "configured": configured, "missing_scopes": missing or [], "reason": reason}

        if publisher_mock_enabled():
            return state(True)
        if not configured:
            return state(False, "Немає облікових даних")
        identifier = self._missing_identifier({})
        if identifier:
            return state(False, f"Не задано {identifier}")
        try:
            token = self._readiness_token()
        except Exception:  # noqa: BLE001 - the provider's text may quote the secret
            return state(False, "Токен недійсний або прострочений: не вдалося оновити")
        if not token:
            return state(False, "Токен недоступний")
        provider = self._token_provider()
        if self.token_scopes() or (provider is not None and provider.scopes_known()):
            missing = self.missing_scopes()
            if missing:
                return state(False, "Недостатньо дозволів: " + ", ".join(missing), missing)
        probe = self._probe_request(token)
        if probe is None:
            return state(True)
        try:
            response = self.transport.get(probe[0], **probe[1], timeout=15)
        except Exception:  # noqa: BLE001 - a transport error can quote the URL with the token
            return state(False, "Платформа недоступна: облікові дані не перевірено")
        if response.status_code in {401, 403}:
            return state(False, "Платформа відхилила облікові дані або дозволи")
        if response.status_code >= 400:
            return state(False, f"Платформа відповіла помилкою {response.status_code}")
        return state(True)

    def token_scopes(self) -> list[str]:
        """Return scopes granted by the current token, or [] if unavailable."""
        provider = self._token_provider() if hasattr(self, "_token_provider") else None
        if provider is None:
            return []
        scopes = getattr(provider, "token_scopes", None)
        if callable(scopes):
            try:
                return list(scopes() or [])
            except Exception:
                return []
        return []

    def _missing_identifier(self, metadata: dict) -> str | None:
        identifiers = {"facebook": ("FACEBOOK_PAGE_ID", ("page_id", "facebook_page_id")),
                       "instagram": ("INSTAGRAM_USER_ID", ("instagram_user_id",)),
                       "threads": ("THREADS_USER_ID", ("threads_user_id",))}
        required = identifiers.get(self.channel)
        if required and not (os.getenv(required[0]) or any(metadata.get(key) for key in required[1])):
            return required[0]
        return None

    def missing_scopes(self) -> list[str]:
        """Return the subset of ``required_scopes`` not granted by the current token."""
        if not self.required_scopes:
            return []
        granted = set(self.token_scopes())
        return [s for s in self.required_scopes if s not in granted]

    def reconnect(self) -> bool:
        """Trigger re-authentication: invalidate cached token and re-fetch.

        Returns ``True`` if a valid token is available after reconnection.
        """
        token_provider = self._token_provider() if hasattr(self, "_token_provider") else None
        if token_provider is None:
            return self.configured()
        try:
            token = token_provider.reconnect() if hasattr(token_provider, "reconnect") else None
            return bool(token)
        except Exception:
            return False

    def publish(self, video_path: str, metadata: dict) -> dict:
        if not self.configured():
            return {
                "channel": self.channel,
                "status": "NOT_CONFIGURED",
                "error": f"{self.credential_env} is missing",
            }
        missing_identifier = self._missing_identifier(metadata)
        if missing_identifier and not publisher_mock_enabled():
            return {"channel": self.channel, "status": "FAILED", "retryable": False,
                    "error": f"{missing_identifier} is required for publishing"}
        # Issue #167.2: unsuitable credentials (missing OAuth scopes) block upload
        if not self.ready(metadata):
            missing = self.missing_scopes()
            if missing:
                return {
                    "channel": self.channel,
                    "status": "NOT_CONFIGURED",
                    "error": f"Missing OAuth scopes: {', '.join(missing)}",
                }
            return {
                "channel": self.channel,
                "status": "NOT_CONFIGURED",
                "error": "Credentials not ready",
            }
        if publisher_mock_enabled():
            return _mock_receipt(self.channel, video_path, metadata)
        try:
            return self._publish_live(video_path, metadata)
        except Exception as error:  # noqa: BLE001 — surface to caller as FAILED
            from core.logging_config import secret_redact
            status = re.search(r"HTTP (\d{3})", str(error))
            retryable = (int(status[1]) in {408, 425, 429} or int(status[1]) >= 500) if status else not isinstance(error, ValueError)
            unknown = isinstance(error, (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                                         httpx.WriteTimeout, httpx.RemoteProtocolError))
            return {"channel": self.channel, "status": "FAILED", "error": secret_redact(str(error)),
                    "retryable": retryable and not unknown, "outcome_unknown": unknown}

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        raise NotImplementedError(
            f"{type(self).__name__} must implement _publish_live()"
        )
