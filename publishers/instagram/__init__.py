"""Instagram official upload adapter (Graph API Reels two-step publish).

Uses ``INSTAGRAM_ACCESS_TOKEN`` and an IG user id (env ``INSTAGRAM_USER_ID`` or
``metadata["instagram_user_id"]``). The file goes through the resumable flow, then
the media container is awaited and published.
"""

from __future__ import annotations

import os
from publishers.oauth import InstagramTokenProvider

from publishers.base import Publisher, check_response, fetch_permalink, wait_for_container

_GRAPH = "https://graph.facebook.com/v21.0"
_UPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"


class InstagramPublisher(Publisher):
    channel = "instagram"
    credential_env = "INSTAGRAM_ACCESS_TOKEN"
    required_scopes = ["instagram_content_publish", "instagram_basic", "pages_show_list"]

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = InstagramTokenProvider().access_token()
        user_id = (
            os.getenv("INSTAGRAM_USER_ID") or metadata.get("instagram_user_id")
        )
        if not user_id:
            raise ValueError("INSTAGRAM_USER_ID is required for Instagram publishing")
        caption = metadata.get("description", "") or metadata.get("topic", "")

        # Resumable upload sends the file itself, so CORE needs no public address.
        create = self.transport.post(
            f"{_GRAPH}/{user_id}/media",
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "access_token": token,
            },
        )
        check_response(create)
        creation_id = create.json().get("id")

        with open(video_path, "rb") as video:
            content = video.read()
        upload = self.transport.post(
            f"{_UPLOAD}/{creation_id}",
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(content))},
            content=content,
            timeout=600,
        )
        check_response(upload)
        wait_for_container(self.transport, f"{_GRAPH}/{creation_id}", token, "status_code")

        publish = self.transport.post(
            f"{_GRAPH}/{user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
        )
        check_response(publish)
        media_id = publish.json().get("id") or creation_id
        # The publish answer has only the media id; the shortcode link is a field.
        permalink = fetch_permalink(self.transport, f"{_GRAPH}/{media_id}", token)
        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": media_id,
            "remote_id": media_id,
            "url": permalink or "",
            **({} if permalink else {"permalink_available": False}),
            "media_id": creation_id,
            "upload": {"mode": "graph-api-reels-resumable", "bytes": len(content)},
        }
