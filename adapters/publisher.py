"""Publisher dispatch registry.

``PUBLISHERS`` maps platform channel -> a configured adapter. Live platform
adapters live in ``publishers/<platform>/`` (see ``publishers`` package);
Telegram remains a first-class adapter here because it is fully implemented.
"""

import os

from publishers import (
    Publisher,
    YoutubePublisher,
    TikTokPublisher,
    FacebookPublisher,
    InstagramPublisher,
    ThreadsPublisher,
)


class TelegramChannelPublisher(Publisher):
    channel, credential_env = "telegram", "TELEGRAM_BOT_TOKEN"

    def publish(self, video_path: str, metadata: dict) -> dict:
        from adapters.telegram import TelegramAdapter

        adapter = TelegramAdapter()
        if not adapter.configured():
            return {"channel": self.channel, "status": "NOT_CONFIGURED",
                    "error": "TELEGRAM_BOT_TOKEN is missing"}
        target = metadata.get("target", "")
        if not target:
            return {"channel": self.channel, "status": "FAILED",
                    "error": "Target channel is required"}
        try:
            if video_path and os.path.isfile(video_path):
                adapter.send_video(target, video_path, metadata.get("topic", ""))
            else:
                adapter.send_message(target, metadata.get("topic", ""))
            return {"channel": self.channel, "status": "PUBLISHED", "target": target}
        except Exception as error:  # noqa: BLE001
            return {"channel": self.channel, "status": "FAILED", "error": str(error)}


PUBLISHERS = {
    publisher.channel: publisher
    for publisher in (
        YoutubePublisher(),
        TikTokPublisher(),
        FacebookPublisher(),
        InstagramPublisher(),
        ThreadsPublisher(),
        TelegramChannelPublisher(),
    )
}


__all__ = [
    "Publisher",
    "YoutubePublisher",
    "TikTokPublisher",
    "FacebookPublisher",
    "InstagramPublisher",
    "ThreadsPublisher",
    "TelegramChannelPublisher",
    "PUBLISHERS",
]
