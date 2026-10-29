"""#361: controlled HTTP OAuth with durable encrypted credential state."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.first_run import ensure_secret_store, _write_encrypted_secrets, set_integration_secret, config_root
from publishers.oauth import YouTubeTokenProvider
from publishers.youtube import YoutubePublisher
from tests.acceptance_provenance import acceptance_provenance
from tests.test_publisher_durable_reconciliation import _PLATFORMS


@pytest.fixture
def oauth(monkeypatch):
    state = {"calls": [], "fail": False, "scope": "https://www.googleapis.com/auth/youtube.upload"}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["calls"].append(self.path)
            body = ({"error": "controlled OAuth failure"} if state["fail"] else
                    {"access_token": f"synthetic-access-{len(state['calls'])}", "refresh_token": "synthetic-refresh-rotated",
                     "expires_in": 3600, "scope": state["scope"]})
            data = json.dumps(body).encode()
            self.send_response(400 if state["fail"] else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(YouTubeTokenProvider, "token_url", base + "/token")
    monkeypatch.setattr(YouTubeTokenProvider, "revoke_url", base + "/revoke")
    for key in ["YOUTUBE_ACCESS_TOKEN", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    stored = ensure_secret_store()
    for key in ["youtube_access_token", "youtube_refresh_token", "youtube_client_id", "youtube_client_secret"]:
        stored.pop(key, None)
    stored.get("publisher_oauth", {}).pop("YOUTUBE_ACCESS_TOKEN", None)
    stored.update(youtube_refresh_token="synthetic-refresh", youtube_client_id="controlled-client", youtube_client_secret="synthetic-client-secret")
    _write_encrypted_secrets(stored)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_refresh_expiry_revoke_reconnect_change_actual_secret_store(oauth):
    provider = YouTubeTokenProvider()
    token = provider.access_token()
    assert token
    stored = ensure_secret_store()
    assert stored["youtube_access_token"] == token
    assert stored["youtube_refresh_token"] == "synthetic-refresh-rotated"
    assert stored["publisher_oauth"]["YOUTUBE_ACCESS_TOKEN"]["scopes"] == [oauth["scope"]]
    assert token not in (config_root() / "secrets.enc.json").read_text()
    calls = len(oauth["calls"])
    assert YouTubeTokenProvider().access_token() == token
    assert len(oauth["calls"]) == calls
    assert YoutubePublisher().ready()
    stored["publisher_oauth"]["YOUTUBE_ACCESS_TOKEN"]["expires_at"] = time.time() - 1
    _write_encrypted_secrets(stored)
    refreshed = YouTubeTokenProvider()
    assert refreshed.access_token() != token
    assert len(oauth["calls"]) == calls + 1
    assert refreshed.revoke()
    revoked = ensure_secret_store()
    assert not revoked.get("youtube_access_token") and not revoked.get("youtube_refresh_token")
    assert revoked["publisher_oauth"]["YOUTUBE_ACCESS_TOKEN"]["revoked"]
    assert not YouTubeTokenProvider().access_token()
    assert not YoutubePublisher().ready()
    set_integration_secret("youtube_refresh_token", "synthetic-refresh-reconnected")
    assert YouTubeTokenProvider().reconnect()
    assert YoutubePublisher().ready()
    assert not ensure_secret_store()["publisher_oauth"]["YOUTUBE_ACCESS_TOKEN"]["revoked"]


def test_known_missing_scopes_and_expired_failed_refresh_block_upload(oauth, tmp_path):
    oauth["scope"] = "unrelated.scope"
    provider = YouTubeTokenProvider()
    assert provider.access_token()
    publisher = YoutubePublisher()
    assert not publisher.ready()
    assert publisher.publish(str(tmp_path / "video.mp4"), {})["status"] == "NOT_CONFIGURED"
    stored = ensure_secret_store()
    stored["publisher_oauth"]["YOUTUBE_ACCESS_TOKEN"]["expires_at"] = time.time() - 1
    _write_encrypted_secrets(stored)
    oauth["fail"] = True
    assert not YouTubeTokenProvider().access_token()
    assert not publisher.ready()


@pytest.mark.parametrize("channel,cls,credential,metadata,responses", _PLATFORMS)
def test_all_platforms_durable_scopes_expiry_revocation_and_replaced_credentials(
        monkeypatch, channel, cls, credential, metadata, responses):
    publisher = cls()
    provider = publisher._token_provider()
    # This matrix checks durable local revocation. The controlled HTTP revoke
    # exchange is covered separately by the oauth-server test above.
    monkeypatch.setattr(provider, "revoke_url", "")
    for name in [credential, provider.refresh_env, provider.client_id_env, provider.client_secret_env]:
        if name:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    token = f"synthetic-{channel}-durable-token"
    for name, value in {"FACEBOOK_PAGE_ID": metadata.get("page_id"),
                        "INSTAGRAM_USER_ID": metadata.get("instagram_user_id"),
                        "THREADS_USER_ID": metadata.get("threads_user_id")}.items():
        if value:
            monkeypatch.setenv(name, value)
    provider._cached_token = token
    provider._token_expires_at = time.time() + 3600
    provider._record_scopes(publisher.required_scopes)
    provider._persist(token)
    assert cls().configured() and cls().ready()
    stored = ensure_secret_store()
    stored["publisher_oauth"][credential]["scopes"] = []
    _write_encrypted_secrets(stored)
    assert not cls().ready()
    stored["publisher_oauth"][credential]["expires_at"] = time.time() - 1
    if provider.refresh_env:
        stored[provider.refresh_env.lower()] = ""
    _write_encrypted_secrets(stored)
    assert not cls().ready()
    provider.revoke()
    assert ensure_secret_store()["publisher_oauth"][credential]["revoked"]
    assert not cls().ready()
    set_integration_secret(credential.lower(), f"synthetic-{channel}-replacement")
    assert cls().reconnect()
    assert cls().ready()
