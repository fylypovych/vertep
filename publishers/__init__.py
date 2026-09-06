"""Live Publisher platform adapters.

Each platform adapter implements the official API/OAuth upload flow behind the
shared ``Publisher`` base and an injectable ``HttpTransport`` (see
``publishers.transport``). ``LIVE_PUBLISHERS`` maps channel -> default adapter.
"""

from publishers.base import Publisher
from publishers.transport import HttpTransport, FakeTransport
from publishers.youtube import YoutubePublisher
from publishers.tiktok import TikTokPublisher
from publishers.facebook import FacebookPublisher
from publishers.instagram import InstagramPublisher
from publishers.threads import ThreadsPublisher

LIVE_PUBLISHERS = {
    publisher.channel: publisher
    for publisher in (
        YoutubePublisher(),
        TikTokPublisher(),
        FacebookPublisher(),
        InstagramPublisher(),
        ThreadsPublisher(),
    )
}

__all__ = [
    "Publisher",
    "HttpTransport",
    "FakeTransport",
    "YoutubePublisher",
    "TikTokPublisher",
    "FacebookPublisher",
    "InstagramPublisher",
    "ThreadsPublisher",
    "LIVE_PUBLISHERS",
]
