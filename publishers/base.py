"""Base class for live Publisher adapters.

A publisher resolves the backend through configuration (``PUBLISHER_MOCK`` or a
platform credential env var), then either returns a mock receipt or performs the
real API upload through an injectable ``HttpTransport``.
"""

from __future__ import annotations

import os
import uuid

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

    def __init__(self, transport=None) -> None:
        self.transport = transport or HttpTransport()

    def configured(self) -> bool:
        mock = os.getenv("PUBLISHER_MOCK", "false").lower() == "true"
        return mock or bool(os.getenv(self.credential_env, ""))

    def publish(self, video_path: str, metadata: dict) -> dict:
        if not self.configured():
            return {
                "channel": self.channel,
                "status": "NOT_CONFIGURED",
                "error": f"{self.credential_env} is missing",
            }
        if os.getenv("PUBLISHER_MOCK", "false").lower() == "true":
            return _mock_receipt(self.channel, video_path, metadata)
        try:
            return self._publish_live(video_path, metadata)
        except Exception as error:  # noqa: BLE001 — surface to caller as FAILED
            return {"channel": self.channel, "status": "FAILED", "error": str(error)}

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        raise NotImplementedError(
            f"{type(self).__name__} must implement _publish_live()"
        )
