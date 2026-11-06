"""Threads official upload adapter (Threads API two-step publish).

Uses ``THREADS_ACCESS_TOKEN`` and a Threads user id (env ``THREADS_USER_ID`` or
``metadata["threads_user_id"]``). Requires a publicly hosted ``video_url`` in
``metadata``: a media container is created, then published via
``threads_publish``.
"""

from __future__ import annotations

import os
from publishers.oauth import ThreadsTokenProvider

from publishers.base import Publisher, check_response, fetch_permalink, wait_for_container

_BASE = "https://graph.threads.com/v1.0"


class ThreadsPublisher(Publisher):
    channel = "threads"
    credential_env = "THREADS_ACCESS_TOKEN"
    required_scopes = ["threads_content_publish", "threads_read"]

    def _probe_request(self, token: str):
        return (f"{_BASE}/{os.getenv('THREADS_USER_ID')}",
                {"params": {"fields": "id", "access_token": token}})

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = ThreadsTokenProvider().access_token()
        user_id = os.getenv("THREADS_USER_ID") or metadata.get("threads_user_id")
        if not user_id:
            raise ValueError("THREADS_USER_ID is required for Threads publishing")
        video_url = metadata.get("video_url")
        if not video_url:
            raise ValueError(
                "Threads завантажує відео за посиланням: вкажіть публічну адресу в Налаштування → Публікація"
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
        wait_for_container(self.transport, f"{_BASE}/{media_id}", token, "status")

        publish = self.transport.post(
            f"{_BASE}/{user_id}/threads_publish",
            data={"creation_id": media_id, "access_token": token},
        )
        check_response(publish)
        post_id = publish.json().get("id") or media_id
        # Threads omits the link for media flagged for copyright.
        permalink = fetch_permalink(self.transport, f"{_BASE}/{post_id}", token)
        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": post_id,
            "remote_id": post_id,
            "url": permalink or "",
            **({} if permalink else {"permalink_available": False}),
            "media_id": media_id,
            "upload": {"mode": "threads-api"},
        }
