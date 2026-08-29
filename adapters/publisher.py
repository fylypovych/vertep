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


PUBLISHERS = {publisher.channel: publisher for publisher in
              (YoutubePublisher(), TikTokPublisher(), FacebookPublisher(), InstagramPublisher(), ThreadsPublisher())}
