"""Facebook official upload adapter (Graph API video upload).

Uses ``FACEBOOK_ACCESS_TOKEN`` and a page id (env ``FACEBOOK_PAGE_ID`` or
``metadata["page_id"]``). Uploads a video to the page via the Graph API
``/{page-id}/videos`` multipart endpoint.
"""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

_GRAPH = "https://graph.facebook.com/v21.0"


class FacebookPublisher(Publisher):
    channel = "facebook"
    credential_env = "FACEBOOK_ACCESS_TOKEN"

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = os.environ[self.credential_env]
        page_id = (
            os.getenv("FACEBOOK_PAGE_ID")
            or metadata.get("page_id")
            or metadata.get("facebook_page_id")
        )
        if not page_id:
            raise ValueError("FACEBOOK_PAGE_ID is required for Facebook publishing")
        description = metadata.get("description", "") or metadata.get("topic", "")
        form = {"access_token": token}
        if description:
            form["description"] = description
        files = None
        if os.path.isfile(video_path):
            files = {"file": ("video.mp4", open(video_path, "rb"))}

        response = self.transport.post(
            f"{_GRAPH}/{page_id}/videos", data=form, files=files
        )
        check_response(response)
        video_id = response.json().get("id")
        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": video_id,
            "url": (
                f"https://www.facebook.com/{page_id}/videos/{video_id}"
                if video_id
                else None
            ),
            "upload": {"mode": "graph-api-multipart"},
        }
