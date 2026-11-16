"""Publisher dispatch registry.

``PUBLISHERS`` maps platform channel -> a configured adapter. Live platform
adapters live in ``publishers/<platform>/`` (see ``publishers`` package);
Telegram uses the same single adapter in ``publishers/telegram`` (var.0.0.4.86).
"""

from publishers import (
    Publisher,
    YoutubePublisher,
    TikTokPublisher,
    FacebookPublisher,
    InstagramPublisher,
    ThreadsPublisher,
)
from publishers.telegram import TelegramPublisher


PUBLISHERS = {
    publisher.channel: publisher
    for publisher in (
        YoutubePublisher(),
        TikTokPublisher(),
        FacebookPublisher(),
        InstagramPublisher(),
        ThreadsPublisher(),
        TelegramPublisher(),
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
