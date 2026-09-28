import os
import types
import pytest
from publishers.oauth import InstagramTokenProvider, TikTokTokenProvider, ThreadsTokenProvider, FacebookTokenProvider

class MockResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
    def json(self):
        return self._json

@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    # Ensure env vars are clean before each test
    for var in [
        "INSTAGRAM_REFRESH_TOKEN", "INSTAGRAM_CLIENT_ID", "INSTAGRAM_CLIENT_SECRET",
        "TIKTOK_REFRESH_TOKEN", "TIKTOK_CLIENT_ID", "TIKTOK_CLIENT_SECRET",
        "THREADS_REFRESH_TOKEN", "THREADS_CLIENT_ID", "THREADS_CLIENT_SECRET",
        "FACEBOOK_ACCESS_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    yield

def test_successful_refresh(monkeypatch):
    # Set up env vars for Instagram provider (as an example)
    monkeypatch.setenv("INSTAGRAM_REFRESH_TOKEN", "dummy_refresh")
    monkeypatch.setenv("INSTAGRAM_CLIENT_ID", "cid")
    monkeypatch.setenv("INSTAGRAM_CLIENT_SECRET", "csecret")
    # Mock the _post method to simulate a successful token refresh
    def mock_post(self, url, data=None, timeout=10):
        assert url == InstagramTokenProvider.token_url
        return MockResponse(200, {"access_token": "new_token", "expires_in": 3600})
    monkeypatch.setattr(InstagramTokenProvider, "_post", mock_post, raising=False)
    provider = InstagramTokenProvider()
    token = provider.access_token()
    assert token == "new_token"
    # Ensure the cached token expires in the future
    assert provider._token_expires_at > 0

def test_failed_refresh_status(monkeypatch):
    monkeypatch.setenv("TIKTOK_REFRESH_TOKEN", "dummy_refresh")
    monkeypatch.setenv("TIKTOK_CLIENT_ID", "cid")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "csecret")
    def mock_post(self, url, data=None, timeout=10):
        return MockResponse(400, {})
    monkeypatch.setattr(TikTokTokenProvider, "_post", mock_post, raising=False)
    provider = TikTokTokenProvider()
    token = provider.access_token()
    assert token == ""  # fallback to empty string on failure

def test_no_refresh_config(monkeypatch):
    # Facebook provider has no refresh support
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "")
    provider = FacebookTokenProvider()
    token = provider.access_token()
    # No credential and no refresh -> empty string
    assert token == ""
