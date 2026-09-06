"""Threads official upload adapter (Threads API two-step publish).

Uses ``THREADS_ACCESS_TOKEN`` and a Threads user id (env ``THREADS_USER_ID`` or
``metadata["threads_user_id"]``). Requires a publicly hosted ``video_url`` in
``metadata``: a media container is created, then published via
``threads_publish``.
"""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

_BASE = "https://graph.threads.net/v1.0"


class ThreadsPublisher(Publisher):
    channel = "threads"
    credential_env = "THREADS_ACCESS_TOKEN"

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = os.environ[self.credential_env]
        user_id = os.getenv("THREADS_USER_ID") or metadata.get("threads_user_id")
        if not user_id:
            raise ValueError("THREADS_USER_ID is required for Threads publishing")
        video_url = metadata.get("video_url")
        if not video_url:
            raise ValueError(
                "Threads require metadata['video_url'] (a hosted video URL)"
            )
        text = metadata.get("description", "") or metadata.get("topic", "")

        create = self.transport.post(
            f"{_BASE}/{user_id}/threads",
            data={
                "media_type": "VIDEO",
                "video_url": video_url,
                "text": text,
                "access_token": token,
            },
        )
        check_response(create)
        media_id = create.json().get("id")

        publish = self.transport.post(
            f"{_BASE}/{user_id}/threads_publish",
            data={"creation_id": media_id, "access_token": token},
        )
        check_response(publish)
        result = publish.json()
        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": result.get("id") or media_id,
            "media_id": media_id,
            "upload": {"mode": "threads-api"},
        }
