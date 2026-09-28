"""Phase 3 tests: live Publisher adapters (mock transport + integration).

These tests exercise the official API request flow for YouTube / TikTok /
Facebook / Instagram / Threads against an injectable ``FakeTransport``, plus
the ``DefaultPublisherProvider`` routing integration.
"""

import httpx

from publishers import (
    FakeTransport,
    FacebookPublisher,
    InstagramPublisher,
    ThreadsPublisher,
    TikTokPublisher,
    YoutubePublisher,
)
from publishers.oauth import YouTubeTokenProvider
from adapters.publisher import PUBLISHERS


def _mp4(tmp_path, name="video.mp4", size=64):
    path = tmp_path / name
    path.write_bytes(b"\x00" * size)
    return str(path)


# ---------------------------------------------------------------------------
# Configuration gates
# ---------------------------------------------------------------------------


def test_live_adapter_not_configured_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    result = YoutubePublisher(transport=FakeTransport([])).publish(
        _mp4(tmp_path), {"topic": "x"}
    )
    assert result["channel"] == "youtube"
    assert result["status"] == "NOT_CONFIGURED"


def test_mock_mode_returns_receipt_for_all_live_channels(monkeypatch, tmp_path):
    video = _mp4(tmp_path)
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    live = [
        YoutubePublisher(transport=FakeTransport([])),
        TikTokPublisher(transport=FakeTransport([])),
        FacebookPublisher(transport=FakeTransport([])),
        InstagramPublisher(transport=FakeTransport([])),
        ThreadsPublisher(transport=FakeTransport([])),
    ]
    for publisher in live:
        result = publisher.publish(video, {"topic": "t"})
        assert result["channel"] == publisher.channel
        assert result["status"] == "PUBLISHED"


# ---------------------------------------------------------------------------
# YouTube resumable upload
# ---------------------------------------------------------------------------


def test_youtube_resumable_upload_flow(monkeypatch, tmp_path):
    video = _mp4(tmp_path, size=128)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "yt-token")
    fake = FakeTransport([
        httpx.Response(200, json={"upload": "created"},
                       headers={"Location": "https://upload.example/seg1"}),
        httpx.Response(200, json={"id": "abc123"}),
    ])
    publisher = YoutubePublisher(transport=fake)

    result = publisher.publish(video, {"title": "Тест", "hashtags": ["a"], "privacy": "unlisted"})

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "abc123"
    assert result["url"] == "https://youtu.be/abc123"
    assert [m for m, *_ in fake.requests] == ["POST", "PUT"]
    method, url, kwargs = fake.requests[0]
    assert url.startswith("https://www.googleapis.com/upload/youtube/v3/videos")
    assert "uploadType=resumable" in url
    assert kwargs["headers"]["Authorization"] == "Bearer yt-token"
    assert kwargs["json"]["snippet"]["title"] == "Тест"
    assert kwargs["json"]["status"]["privacyStatus"] == "unlisted"
    _, _, upload_kwargs = fake.requests[1]
    assert upload_kwargs["headers"]["Content-Length"] == "128"
    assert upload_kwargs["content"] == b"\x00" * 128

# ---------------------------------------------------------------------------
# Facebook Graph API
# ---------------------------------------------------------------------------


def test_facebook_graph_api_upload(monkeypatch, tmp_path):
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "fb-token")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "page-7")
    fake = FakeTransport([httpx.Response(200, json={"id": "vid-99"})])
    publisher = FacebookPublisher(transport=fake)

    result = publisher.publish(_mp4(tmp_path), {"description": "hi"})

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "vid-99"
    assert result["url"] == "https://www.facebook.com/page-7/videos/vid-99"
    method, url, kwargs = fake.requests[0]
    assert method == "POST"
    assert url == "https://graph.facebook.com/v21.0/page-7/videos"
    assert kwargs["data"]["access_token"] == "fb-token"
    assert kwargs["data"]["description"] == "hi"
    assert "file" in kwargs["files"]


def test_facebook_requires_page_id(monkeypatch, tmp_path):
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "fb-token")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    result = FacebookPublisher(transport=FakeTransport([])).publish(
        _mp4(tmp_path), {}
    )
    assert result["status"] == "FAILED"
    assert "FACEBOOK_PAGE_ID" in result["error"]


# ---------------------------------------------------------------------------
# Instagram Graph API (Reels, two-step)
# ---------------------------------------------------------------------------


def test_instagram_reels_two_step(monkeypatch, tmp_path):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    fake = FakeTransport([
        httpx.Response(200, json={"id": "container-1"}),
        httpx.Response(200, json={"id": "media-55"}),
    ])
    publisher = InstagramPublisher(transport=fake)

    result = publisher.publish(_mp4(tmp_path), {
        "instagram_user_id": "ig-9", "video_url": "https://cdn.example/v.mp4",
    })

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "media-55"
    assert result["media_id"] == "container-1"
    assert "media_publish" in fake.requests[1][1]
    post_data = fake.requests[1][2]["data"]
    assert post_data["creation_id"] == "container-1"


def test_instagram_requires_video_url(monkeypatch, tmp_path):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    result = InstagramPublisher(transport=FakeTransport([])).publish(
        _mp4(tmp_path), {"instagram_user_id": "ig-9"}
    )
    assert result["status"] == "FAILED"
    assert "video_url" in result["error"]


# ---------------------------------------------------------------------------
# Threads API (two-step)
# ---------------------------------------------------------------------------


def test_threads_two_step(monkeypatch, tmp_path):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "th-token")
    fake = FakeTransport([
        httpx.Response(200, json={"id": "thread-media-1"}),
        httpx.Response(200, json={"id": "thread-12"}),
    ])
    publisher = ThreadsPublisher(transport=fake)

    result = publisher.publish(_mp4(tmp_path), {
        "threads_user_id": "th-3", "video_url": "https://cdn.example/v.mp4",
        "description": "пост",
    })

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "thread-12"
    create_url = fake.requests[0][1]
    assert "graph.threads.net" in create_url and create_url.endswith("/th-3/threads")
    create_data = fake.requests[0][2]["data"]
    assert create_data["media_type"] == "VIDEO"
    assert create_data["text"] == "пост"
    assert "threads_publish" in fake.requests[1][1]


# ---------------------------------------------------------------------------
# TikTok Content Posting API (three-step)
# ---------------------------------------------------------------------------


def test_tiktok_three_step(monkeypatch, tmp_path):
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tk-token")
    video = _mp4(tmp_path, size=256)
    fake = FakeTransport([
        httpx.Response(200, json={"data": {
            "upload_url": "https://upload.tiktok.example/seg",
            "publish_id": "pub-1",
        }}),
        httpx.Response(200, json={"status_code": 0}),
        httpx.Response(200, json={"data": {"status": "SEND_TO_USER_FEED"}}),
    ])
    publisher = TikTokPublisher(transport=fake)

    result = publisher.publish(video, {"title": "танок", "privacy": "public"})

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "pub-1"
    methods = [m for m, *_ in fake.requests]
    assert methods == ["POST", "PUT", "POST"]
    init_kwargs = fake.requests[0][2]
    assert init_kwargs["json"]["post_info"]["privacy_level"] == "PUBLIC"
    assert init_kwargs["json"]["source_info"]["video_size"] == 256
    upload_kwargs = fake.requests[1][2]
    assert upload_kwargs["headers"]["Content-Length"] == "256"
    status_kwargs = fake.requests[2][2]
    assert status_kwargs["json"] == {"publish_id": "pub-1"}
    assert result["status_info"]["status"] == "SEND_TO_USER_FEED"


def test_tiktok_requires_local_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tk-token")
    result = TikTokPublisher(transport=FakeTransport([])).publish(
        str(tmp_path / "missing.mp4"), {}
    )
    assert result["status"] == "FAILED"
    assert "existing local video" in result["error"]


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


def test_api_error_surfaces_as_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "fb-token")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "page-7")
    fake = FakeTransport([httpx.Response(400, json={"error": {"message": "bad token"}})])
    result = FacebookPublisher(transport=fake).publish(_mp4(tmp_path), {})
    assert result["status"] == "FAILED"
    assert "400" in result["error"]


# ---------------------------------------------------------------------------
# DefaultPublisherProvider integration (routing to live adapter)
# ---------------------------------------------------------------------------


def test_provider_routes_to_live_adapter(monkeypatch, tmp_path):
    from adapters.providers import DefaultPublisherProvider

    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "yt-token")
    fake = FakeTransport([
        httpx.Response(200, json={}, headers={"Location": "https://u.example/1"}),
        httpx.Response(200, json={"id": "vid-live"}),
    ])
    patched = dict(PUBLISHERS)
    patched["youtube"] = YoutubePublisher(transport=fake)
    monkeypatch.setattr("adapters.publisher.PUBLISHERS", patched)

    provider = DefaultPublisherProvider()
    result = provider.publish("youtube", _mp4(tmp_path), {"topic": "x"})

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "vid-live"


# ---------------------------------------------------------------------------
# OAuth token refresh lifecycle
# ---------------------------------------------------------------------------


def test_youtube_refresh_token_refreshes_on_401(monkeypatch, tmp_path):
    """On 401, YouTube publisher uses the refresh token to get a new access token and retries."""
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "refresh-123")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "expired-token")
    fake = FakeTransport([
        httpx.Response(401, json={"error": "Invalid Credentials"}),
        httpx.Response(200, json={"access_token": "refreshed-token", "expires_in": 3600,
                                  "scope": "https://www.googleapis.com/auth/youtube.upload", "token_type": "Bearer"}),
        httpx.Response(200, json={}, headers={"Location": "https://u.example/1"}),
        httpx.Response(200, json={"id": "vid-401"}),
    ])
    publisher = YoutubePublisher(transport=fake)
    result = publisher.publish(_mp4(tmp_path, size=128), {"title": "Тест"})

    assert result["status"] == "PUBLISHED"
    assert result["id"] == "vid-401"
    methods = [m for m, *_ in fake.requests]
    assert methods == ["POST", "POST", "POST", "PUT"]
    token_req = fake.requests[1]
    assert token_req[2]["data"]["refresh_token"] == "refresh-123"
    assert token_req[2]["data"]["grant_type"] == "refresh_token"
    upload_headers = fake.requests[3][2]["headers"]
    assert upload_headers["Authorization"] == "Bearer refreshed-token"


def test_youtube_no_refresh_token_returns_401_error(monkeypatch, tmp_path):
    """Without a refresh token, a 401 surfaces as FAILED."""
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "expired-token")
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
    fake = FakeTransport([
        httpx.Response(401, json={"error": "Invalid Credentials"}),
    ])
    publisher = YoutubePublisher(transport=fake)
    result = publisher.publish(_mp4(tmp_path), {"topic": "x"})
    assert result["status"] == "FAILED"
    assert "401" in result["error"]


# ---------------------------------------------------------------------------
# Credential readiness, scopes, and revocation
# ---------------------------------------------------------------------------


def test_publisher_not_ready_without_credentials(monkeypatch):
    """A publisher with no credentials is not ready."""
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TIKTOK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FACEBOOK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    for cls in (YoutubePublisher, TikTokPublisher, FacebookPublisher,
                InstagramPublisher, ThreadsPublisher):
        pub = cls(transport=FakeTransport([]))
        assert pub.configured() is False
        assert pub.ready() is False


def test_publisher_ready_with_token(monkeypatch):
    """A publisher with a token env var is ready."""
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "test-token")
    pub = YoutubePublisher(transport=FakeTransport([]))
    assert pub.configured() is True
    assert pub.ready() is True


def test_publisher_mock_is_ready_and_configured(monkeypatch):
    """In PUBLISHER_MOCK mode all publishers are ready."""
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    pub = YoutubePublisher(transport=FakeTransport([]))
    assert pub.configured() is True
    assert pub.ready() is True


def test_required_scopes_defined_for_all_platforms():
    """Every platform publisher declares required OAuth scopes."""
    for cls in (YoutubePublisher, TikTokPublisher, FacebookPublisher,
                InstagramPublisher, ThreadsPublisher):
        assert len(cls.required_scopes) > 0, f"{cls.channel} missing required_scopes"


def test_missing_scopes_empty_when_no_token(monkeypatch):
    """When no token is present, missing_scopes returns required_scopes."""
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    pub = YoutubePublisher(transport=FakeTransport([]))
    missing = pub.missing_scopes()
    assert set(missing) == set(YoutubePublisher.required_scopes)


def test_revoke_clears_cached_token(monkeypatch):
    """revoke() clears the locally cached token."""
    tp = YouTubeTokenProvider(transport=FakeTransport([]))
    tp._cached_token = "some-token"
    tp._token_expires_at = 9999999999
    assert tp.revoke() is False  # no revoke_url configured on base test context
    assert tp._cached_token is None


def test_youtube_revoke_calls_endpoint(monkeypatch):
    """YouTube revoke hits the Google revocation endpoint."""
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "refresh-xyz")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csec")
    fake = FakeTransport([httpx.Response(200, json={"success": True})])
    tp = YouTubeTokenProvider(transport=fake)
    tp._cached_token = "access-xyz"
    tp._token_expires_at = 9999999999
    assert tp.revoke() is True
    method, url, kwargs = fake.requests[0]
    assert url == "https://oauth2.googleapis.com/revoke"
    assert kwargs["data"]["token"] == "refresh-xyz"
    assert kwargs["data"]["token_type_hint"] == "refresh_token"


def test_youtube_reconnect_clears_and_refetches(monkeypatch):
    """reconnect() clears the cached token and re-fetches from env."""
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "new-token-from-env")
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    tp = YouTubeTokenProvider(transport=FakeTransport([]))
    tp._cached_token = "old-token"
    tp._token_expires_at = 9999999999
    assert tp.reconnect() == "new-token-from-env"
    assert tp._cached_token == "new-token-from-env"


def test_publisher_reconnect_returns_true_when_token_available(monkeypatch):
    """Publisher.reconnect() delegates to token provider."""
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "fresh-token")
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    pub = YoutubePublisher(transport=FakeTransport([]))
    assert pub.reconnect() is True


def test_publisher_reconnect_returns_false_without_credentials(monkeypatch):
    """Publisher.reconnect() returns False when no credentials are available."""
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    pub = YoutubePublisher(transport=FakeTransport([]))
    assert pub.reconnect() is False


# ---------------------------------------------------------------------------
# End-to-end: approved video to worker execute_publisher to receipt for all 5 platforms
# ---------------------------------------------------------------------------


def test_end_to_end_publish_receives_receipt_for_all_platforms(monkeypatch, tmp_path):
    """A full publish round-trip: approved video to PublisherWorker.execute_publisher to receipt.

    Each platform is given appropriate credentials and a FakeTransport that
    simulates the real API response flow. The test asserts that each platform
    produces a PUBLISHED receipt with the expected platform fields, proving the
    end-to-end contract for all five live adapters.
    """
    import base64
    import json
    from worker.role_executor import execute_publisher

    video = _mp4(tmp_path, size=1024)
    delivery_contract = {
        "job_id": "job-e2e", "video_version": "v1", "video_sha256": "abc123",
        "channel": "youtube",
    }
    publish_intent = {"video_version": "v1", "video_sha256": "abc123"}

    platforms = {
        "youtube": (
            YoutubePublisher(transport=FakeTransport([
                httpx.Response(200, json={},
                               headers={"Location": "https://upload.example/init"}),
                httpx.Response(200, json={"id": "yt-vid-001"}),
            ])),
            {"YOUTUBE_ACCESS_TOKEN": "yt-token"},
        ),
        "tiktok": (
            TikTokPublisher(transport=FakeTransport([
                httpx.Response(200, json={"data": {
                    "upload_url": "https://upload.tiktok.example/seg",
                    "publish_id": "tk-pub-002",
                }}),
                httpx.Response(200, json={"status_code": 0}),
                httpx.Response(200, json={"data": {"status": "SEND_TO_USER_FEED"}}),
            ])),
            {"TIKTOK_ACCESS_TOKEN": "tk-token"},
        ),
        "facebook": (
            FacebookPublisher(transport=FakeTransport([
                httpx.Response(200, json={"id": "fb-vid-003"}),
            ])),
            {"FACEBOOK_ACCESS_TOKEN": "fb-token", "FACEBOOK_PAGE_ID": "page-123"},
        ),
        "instagram": (
            InstagramPublisher(transport=FakeTransport([
                httpx.Response(200, json={"id": "ig-container-004"}),
                httpx.Response(200, json={"id": "ig-media-004"}),
            ])),
            {"INSTAGRAM_ACCESS_TOKEN": "ig-token"},
        ),
        "threads": (
            ThreadsPublisher(transport=FakeTransport([
                httpx.Response(200, json={"id": "th-media-005"}),
                httpx.Response(200, json={"id": "th-005"}),
            ])),
            {"THREADS_ACCESS_TOKEN": "th-token"},
        ),
    }

    patched = {name: pub for name, (pub, _) in platforms.items()}
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", patched)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    for name, (_, env_vars) in platforms.items():
        for key, val in env_vars.items():
            monkeypatch.setenv(key, val)

    metadata = {
        "topic": "E2E test", "title": "Test video",
        "instagram_user_id": "ig-9", "threads_user_id": "th-3",
        "video_url": "https://cdn.example/v.mp4",
    }

    for channel, (publisher, _) in platforms.items():
        task = {
            "job_id": "job-e2e",
            "task_id": f"task-{channel}",
            "channel": channel,
            "video_path": video,
            "metadata": metadata,
            "delivery_contract": delivery_contract,
            "publish_intent": publish_intent,
            "node_name": "publisher-1",
        }
        artifacts = execute_publisher(task)
        assert len(artifacts) == 1
        assert artifacts[0]["kind"] == "publication_receipt"
        receipt = json.loads(base64.b64decode(artifacts[0]["data_base64"]).decode("utf-8"))
        assert receipt["channel"] == channel
        assert receipt["status"] == "PUBLISHED", f"{channel}: {receipt.get('error')}"
        assert "video_version" in receipt
        assert "video_sha256" in receipt
        assert "timestamp" in receipt


def test_end_to_end_partial_failure_one_platform_fails(monkeypatch, tmp_path):
    """End-to-end: 4 platforms succeed, 1 (instagram) returns FAILED.

    Proves partial failure is handled: each platform is dispatched independently,
    a FAILED receipt for one platform does not stop the others.
    """
    import base64
    import json
    from worker.role_executor import execute_publisher

    video = _mp4(tmp_path, size=512)
    publish_intent = {"video_version": "v1", "video_sha256": "abc123"}

    # Instagram returns FAILED (e.g. API error), others succeed
    instagram_pub = InstagramPublisher(transport=FakeTransport([
        httpx.Response(500, json={"error": "server error"}),
    ]))

    platforms = {
        "youtube": (
            YoutubePublisher(transport=FakeTransport([
                httpx.Response(200, json={},
                               headers={"Location": "https://upload.example/init"}),
                httpx.Response(200, json={"id": "yt-pf-001"}),
            ])),
            {"YOUTUBE_ACCESS_TOKEN": "yt-token"},
        ),
        "tiktok": (
            TikTokPublisher(transport=FakeTransport([
                httpx.Response(200, json={"data": {
                    "upload_url": "https://upload.tiktok.example/seg",
                    "publish_id": "tk-pf-002",
                }}),
                httpx.Response(200, json={"status_code": 0}),
                httpx.Response(200, json={"data": {"status": "SEND_TO_USER_FEED"}}),
            ])),
            {"TIKTOK_ACCESS_TOKEN": "tk-token"},
        ),
        "facebook": (
            FacebookPublisher(transport=FakeTransport([
                httpx.Response(200, json={"id": "fb-pf-003"}),
            ])),
            {"FACEBOOK_ACCESS_TOKEN": "fb-token", "FACEBOOK_PAGE_ID": "page-x"},
        ),
        "instagram": (instagram_pub, {"INSTAGRAM_ACCESS_TOKEN": "ig-token"}),
        "threads": (
            ThreadsPublisher(transport=FakeTransport([
                httpx.Response(200, json={"id": "th-media-pf"}),
                httpx.Response(200, json={"id": "th-pf-005"}),
            ])),
            {"THREADS_ACCESS_TOKEN": "th-token"},
        ),
    }

    patched = {name: pub for name, (pub, _) in platforms.items()}
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", patched)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    for _, env_vars in platforms.values():
        for key, val in env_vars.items():
            monkeypatch.setenv(key, val)

    metadata = {
        "topic": "Partial test", "instagram_user_id": "ig-9",
        "threads_user_id": "th-3", "video_url": "https://cdn.example/v.mp4",
    }

    results = {}
    for channel, (publisher, _) in platforms.items():
        task = {
            "job_id": "job-pf",
            "task_id": f"task-{channel}",
            "channel": channel,
            "video_path": video,
            "metadata": metadata,
            "delivery_contract": {},
            "publish_intent": publish_intent,
            "node_name": "publisher-1",
        }
        artifacts = execute_publisher(task)
        receipt = json.loads(
            base64.b64decode(artifacts[0]["data_base64"]).decode("utf-8")
        )
        results[channel] = receipt

    assert results["youtube"]["status"] == "PUBLISHED"
    assert results["tiktok"]["status"] == "PUBLISHED"
    assert results["facebook"]["status"] == "PUBLISHED"
    assert results["threads"]["status"] == "PUBLISHED"
    assert results["instagram"]["status"] == "FAILED"
    assert "super-secret-token" not in results["instagram"].get("error", "")
    """Errors surfacing from execute_publisher must not leak credential values."""
    import json
    import base64
    from worker.role_executor import execute_publisher

    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "super-secret-token-123")
    monkeypatch.setenv("PUBLISHER_MOCK", "false")

    class LeakingPublisher:
        channel = "youtube"
        credential_env = "YOUTUBE_ACCESS_TOKEN"

        def configured(self) -> bool:
            return True

        def publish(self, video_path, metadata):
            return {
                "channel": "youtube",
                "status": "FAILED",
                "error": "Bearer super-secret-token-123 rejected",
            }

    monkeypatch.setattr(
        "publishers.LIVE_PUBLISHERS", {"youtube": LeakingPublisher()}
    )

    task = {
        "job_id": "job-redact",
        "task_id": "task-redact",
        "channel": "youtube",
        "video_path": _mp4(tmp_path),
        "metadata": {"topic": "x"},
        "delivery_contract": {},
        "publish_intent": {},
        "node_name": "publisher-1",
    }
    artifacts = execute_publisher(task)
    receipt = json.loads(
        base64.b64decode(artifacts[0]["data_base64"]).decode("utf-8")
    )
    assert receipt["status"] == "FAILED"
    assert "super-secret-token-123" not in receipt["error"]