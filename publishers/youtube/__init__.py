"""YouTube official upload adapter (resumable upload protocol).

Uses ``YOUTUBE_ACCESS_TOKEN`` for direct uploads.  When
``YOUTUBE_REFRESH_TOKEN`` is also set (optionally read from the integration
secret store), the adapter transparently refreshes an expired access token
on 401 responses and retries the upload once.
"""

from __future__ import annotations

import os
import time

from publishers.base import Publisher, check_response
from publishers.transport import HttpTransport

_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class _TokenProvider:
    """Resolves and refreshes YouTube OAuth tokens.

    Token priority: ``YOUTUBE_ACCESS_TOKEN`` env → integration secret.
    Refresh requires ``YOUTUBE_REFRESH_TOKEN`` (or secret),
    ``YOUTUBE_CLIENT_ID`` (or secret) and ``YOUTUBE_CLIENT_SECRET`` (or secret).
    """

    credential_env = "YOUTUBE_ACCESS_TOKEN"

    def __init__(self, transport: HttpTransport | None = None) -> None:
        self._transport = transport or HttpTransport()
        self._cached_token: str | None = None
        self._token_expires_at: float = 0

    def _get_secret(self, name: str) -> str | None:
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
            }.get(name, name.lower())
            return store.get(secret_name)
        except Exception:
            return None

    def access_token(self) -> str:
        if self._cached_token and time.time() < self._token_expires_at:
            return self._cached_token
        token = self._get_secret("YOUTUBE_ACCESS_TOKEN") or ""
        if token:
            self._cached_token = token
            self._token_expires_at = time.time() + 3500
        return token

    def refresh_token(self) -> str | None:
        return self._get_secret("YOUTUBE_REFRESH_TOKEN")

    def try_refresh(self) -> str | None:
        refresh = self.refresh_token()
        if not refresh:
            return None
        client_id = self._get_secret("YOUTUBE_CLIENT_ID")
        client_secret = self._get_secret("YOUTUBE_CLIENT_SECRET")
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        try:
            resp = self._transport.post(_TOKEN_URL, data=data, timeout=10)
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


class YoutubePublisher(Publisher):
    channel = "youtube"
    credential_env = "YOUTUBE_ACCESS_TOKEN"

    def _token_provider(self) -> _TokenProvider:
        return _TokenProvider(self.transport)

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        tp = self._token_provider()
        token = tp.access_token()
        if not token:
            return {"channel": self.channel, "status": "NOT_CONFIGURED",
                    "error": f"{self.credential_env} is missing"}

        body = {
            "snippet": {
                "title": metadata.get("title") or metadata.get("topic") or "Vertep video",
                "description": metadata.get("description", ""),
                "tags": metadata.get("hashtags", []) or [],
                "categoryId": str(metadata.get("category_id", "22")),
            },
            "status": {"privacyStatus": metadata.get("privacy", "private")},
        }
        size = os.path.getsize(video_path) if os.path.isfile(video_path) else 0

        init_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
        }
        if size:
            init_headers["X-Upload-Content-Length"] = str(size)
        init = self.transport.post(
            f"{_UPLOAD_URL}?uploadType=resumable",
            headers=init_headers,
            json=body,
        )

        if init.status_code in (401, 403) and tp.refresh_token():
            new_token = tp.try_refresh()
            if new_token:
                init_headers["Authorization"] = f"Bearer {new_token}"
                init = self.transport.post(
                    f"{_UPLOAD_URL}?uploadType=resumable",
                    headers=init_headers,
                    json=body,
                )

        check_response(init)
        location = init.headers["Location"]

        video = b""
        if os.path.isfile(video_path):
            with open(video_path, "rb") as handle:
                video = handle.read()
        upload_headers = {
            "Authorization": f"Bearer {tp.access_token()}",
            "Content-Type": "video/mp4",
        }
        if size:
            upload_headers["Content-Length"] = str(size)
        final = self.transport.put(
            location, headers=upload_headers, content=video
        )
        check_response(final)
        video_id = final.json().get("id")
        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": video_id,
            "url": f"https://youtu.be/{video_id}" if video_id else None,
            "upload": {"mode": "resumable", "bytes": size},
        }
