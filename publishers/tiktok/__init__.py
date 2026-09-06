"""TikTok official upload adapter (Content Posting API, FILE_UPLOAD flow).

Uses ``TIKTOK_ACCESS_TOKEN`` (optionally ``TIKTOK_OPEN_ID`` and the signature
header from env ``TIKTOK_X_TT_POST`` when the API requires it).

The Content Posting API uploads in three steps: ``video/init/`` returns an
upload URL and publish id, the raw bytes are PUT to that URL, then
``status/fetch/`` is polled for the final state.
"""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

_INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_STATUS = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"


class TikTokPublisher(Publisher):
    channel = "tiktok"
    credential_env = "TIKTOK_ACCESS_TOKEN"

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        token = os.environ[self.credential_env]
        if not os.path.isfile(video_path):
            raise ValueError("TikTok upload requires an existing local video file")
        size = os.path.getsize(video_path)

        init_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        if os.getenv("TIKTOK_X_TT_POST"):
            init_headers["X-Tt-Post"] = os.getenv("TIKTOK_X_TT_POST")

        privacy = metadata.get("privacy", "SELF_ONLY").upper()
        init_payload = {
            "post_info": {
                "title": metadata.get("title") or metadata.get("topic") or "Vertep video",
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        }
        init = self.transport.post(_INIT, headers=init_headers, json=init_payload)
        check_response(init)
        data = init.json().get("data", {})
        upload_url = data.get("upload_url")
        publish_id = data.get("publish_id")
        if not upload_url or not publish_id:
            raise ValueError(
                "TikTok init did not return upload_url/publish_id; "
                f"API data: {data}"
            )
        with open(video_path, "rb") as handle:
            video = handle.read()
        upload_headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
        }
        upload = self.transport.put(upload_url, headers=upload_headers, content=video)
        check_response(upload)

        status_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        if os.getenv("TIKTOK_X_TT_POST"):
            status_headers["X-Tt-Post"] = os.getenv("TIKTOK_X_TT_POST")
        status = self.transport.post(
            _STATUS, headers=status_headers, json={"publish_id": publish_id}
        )
        check_response(status)
        status_info = status.json().get("data", {})

        return {
            "channel": self.channel,
            "status": "PUBLISHED",
            "id": publish_id,
            "upload": {
                "mode": "content-posting-api",
                "bytes": size,
                "chunks": 1,
            },
            "status_info": status_info,
        }
