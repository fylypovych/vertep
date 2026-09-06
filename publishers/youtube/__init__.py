"""YouTube official upload adapter (resumable upload protocol).

Uses ``YOUTUBE_ACCESS_TOKEN``. The flow follows the YouTube Data API v3
resumable upload: initiate with the video metadata, then PUT the raw bytes to
the returned upload URL.
"""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YoutubePublisher(Publisher):
    channel = "youtube"
    credential_env = "YOUTUBE_ACCESS_TOKEN"

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = os.environ[self.credential_env]
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
        check_response(init)
        location = init.headers["Location"]

        video = b""
        if os.path.isfile(video_path):
            with open(video_path, "rb") as handle:
                video = handle.read()
        upload_headers = {
            "Authorization": f"Bearer {token}",
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
