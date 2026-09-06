"""Instagram official upload adapter (Graph API Reels two-step publish).

Uses ``INSTAGRAM_ACCESS_TOKEN`` and an IG user id (env ``INSTAGRAM_USER_ID`` or
``metadata["instagram_user_id"]``). Requires a publicly hosted ``video_url`` in
``metadata``: Vertep Reels are published by first creating a media container,
then publishing it.
"""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

_GRAPH = "https://graph.facebook.com/v21.0"


class InstagramPublisher(Publisher):
    channel = "instagram"
    credential_env = "INSTAGRAM_ACCESS_TOKEN"

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = os.environ[self.credential_env]
        user_id = (
            os.getenv("INSTAGRAM_USER_ID") or metadata.get("instagram_user_id")
        )
        if not user_id:
            raise ValueError("INSTAGRAM_USER_ID is required for Instagram publishing")
        video_url = metadata.get("video_url")
        if not video_url:
            raise ValueError(
                "Instagram Reels require metadata['video_url'] (a hosted video URL)"
            )
        caption = metadata.get("description", "") or metadata.get("topic", "")

        create = self.transport.post(
            f"{_GRAPH}/{user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": token,
            },
        )
        check_response(create)
        creation_id = create.json().get("id")

        publish = self.transport.post(
            f"{_GRAPH}/{user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
        )
        check_response(publish)
        result = publish.json()
        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": result.get("id") or creation_id,
            "media_id": creation_id,
            "upload": {"mode": "graph-api-reels"},
        }
