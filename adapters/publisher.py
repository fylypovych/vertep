import os
import uuid


class Publisher:
    channel = "unknown"
    credential_env = ""

    def configured(self) -> bool:
        return os.getenv("PUBLISHER_MOCK", "false").lower() == "true" or bool(os.getenv(self.credential_env, ""))

    def publish(self, video_path: str, metadata: dict) -> dict:
        if not self.configured():
            return {"channel": self.channel, "status": "NOT_CONFIGURED", "error": f"{self.credential_env} is missing"}
        if os.getenv("PUBLISHER_MOCK", "false").lower() == "true":
            publication_id = f"mock-{uuid.uuid4().hex[:12]}"
            return {"channel": self.channel, "status": "PUBLISHED", "id": publication_id,
                    "url": f"https://example.invalid/{self.channel}/{publication_id}",
                    "upload": {"mode": "mock-resumable", "bytes": os.path.getsize(video_path) if os.path.isfile(video_path) else 0,
                               "parts": 1}, "metadata": metadata}
        return {"channel": self.channel, "status": "FAILED", "error": "Live API adapter is not implemented"}


class YoutubePublisher(Publisher):
    channel, credential_env = "youtube", "YOUTUBE_ACCESS_TOKEN"

class TikTokPublisher(Publisher):
    channel, credential_env = "tiktok", "TIKTOK_ACCESS_TOKEN"

class FacebookPublisher(Publisher):
    channel, credential_env = "facebook", "FACEBOOK_ACCESS_TOKEN"

class InstagramPublisher(Publisher):
    channel, credential_env = "instagram", "INSTAGRAM_ACCESS_TOKEN"

class ThreadsPublisher(Publisher):
    channel, credential_env = "threads", "THREADS_ACCESS_TOKEN"


class TelegramChannelPublisher(Publisher):
    channel, credential_env = "telegram", "TELEGRAM_BOT_TOKEN"

    def publish(self, video_path: str, metadata: dict) -> dict:
        from adapters.telegram import TelegramAdapter
        adapter = TelegramAdapter()
        if not adapter.configured():
            return {"channel": self.channel, "status": "NOT_CONFIGURED", "error": "TELEGRAM_BOT_TOKEN is missing"}
        target = metadata.get("target", "")
        if not target:
            return {"channel": self.channel, "status": "FAILED", "error": "Target channel is required"}
        try:
            if video_path and os.path.isfile(video_path):
                adapter.send_video(target, video_path, metadata.get("topic", ""))
            else:
                adapter.send_message(target, metadata.get("topic", ""))
            return {"channel": self.channel, "status": "PUBLISHED", "target": target}
        except Exception as error:
            return {"channel": self.channel, "status": "FAILED", "error": str(error)}


PUBLISHERS = {publisher.channel: publisher for publisher in
              (YoutubePublisher(), TikTokPublisher(), FacebookPublisher(), InstagramPublisher(), ThreadsPublisher(), TelegramChannelPublisher())}
