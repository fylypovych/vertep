"""i.0.0.4.82 (#482): cookie Secure flag, 0600 key from creation, per-client setup limit."""

import base64
import hashlib
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import core.api.auth as auth
import core.first_run as first_run
from core.app import app
from core.state import setup_request_windows

SETUP_CODE = b"setup-hardening-code"


# ── secret-store key permissions ─────────────────────────────────────────────

@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    for name in ("SECRET_STORE_PASSPHRASE", "SECRET_STORE_PASSPHRASE_FILE", "REQUIRE_SECRET_KEY_SEALING"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("passphrase", [None, "a-long-enough-passphrase"])
def test_key_file_is_0600_from_its_creation(config, monkeypatch, passphrase):
    if passphrase:
        monkeypatch.setenv("SECRET_STORE_PASSPHRASE", passphrase)
    observed = []
    real_open = os.open

    def watching_open(path, flags, mode=0o777, *args, **kwargs):
        descriptor = real_open(path, flags, mode, *args, **kwargs)
        observed.append((flags & os.O_EXCL, mode, stat.S_IMODE(os.fstat(descriptor).st_mode)))
        return descriptor

    monkeypatch.setattr(first_run.os, "open", watching_open)
    previous = os.umask(0)  # the most permissive umask: no window may appear
    try:
        key = first_run._secret_key()
    finally:
        os.umask(previous)
    assert observed and all(excl and mode == 0o600 and actual == 0o600 for excl, mode, actual in observed)
    path = config / "secret-store.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(key) == 32
    if passphrase:
        assert path.read_text().lstrip().startswith("{")  # sealing is kept
    assert first_run._secret_key() == key  # never overwritten or lost
    assert not list(config.glob(".secret-store.key*.tmp"))


def test_existing_raw_key_is_sealed_with_0600(config, monkeypatch):
    raw = os.urandom(32)
    (config / "secret-store.key").write_text(base64.urlsafe_b64encode(raw).decode())
    os.chmod(config / "secret-store.key", 0o644)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "a-long-enough-passphrase")
    assert first_run._secret_key() == raw
    assert stat.S_IMODE((config / "secret-store.key").stat().st_mode) == 0o600
    assert first_run._secret_key() == raw


# ── session / CSRF cookies ──────────────────────────────────────────────────

def _login(headers=None):
    credentials = base64.b64encode(b"admin:hardening-password").decode()
    response = TestClient(app).post("/api/session", headers={"Authorization": f"Basic {credentials}",
                                                             **(headers or {})})
    assert response.status_code == 200, response.text
    return {cookie.split("=", 1)[0]: cookie for cookie in response.headers.get_list("set-cookie")}


@pytest.fixture
def admin(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    (tmp_path / "installation.json").write_text('{"completed_at":"2026-01-01"}')
    (tmp_path / "users.json").write_text("{}")
    monkeypatch.setenv("ADMIN_PASSWORD", "hardening-password")


def test_cookies_behind_the_https_proxy_are_secure(admin):
    cookies = _login({"X-Forwarded-Proto": "https"})
    for name in ("vertep_session", "vertep_csrf"):
        assert "secure" in cookies[name].lower(), cookies[name]
    assert "httponly" in cookies["vertep_session"].lower()
    assert "samesite=strict" in cookies["vertep_csrf"].lower()


def test_plain_http_dev_flow_keeps_working_without_secure(admin):
    cookies = _login()
    assert all("secure" not in value.lower() for value in cookies.values())


# ── setup attempt budget ─────────────────────────────────────────────────────

def _client_from(peer: str) -> TestClient:
    async def with_peer(scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "client": (peer, 40000)}
        await app(scope, receive, send)
    return TestClient(with_peer)


@pytest.fixture
def setup_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("SETUP_TOKEN_HASH", hashlib.sha256(SETUP_CODE).hexdigest())
    monkeypatch.delenv("SETUP_TOKEN_EXPIRES_AT", raising=False)
    monkeypatch.setenv("SETUP_RATE_LIMIT_PER_10_MINUTES", "3")
    monkeypatch.setenv("SETUP_RATE_LIMIT_GLOBAL_PER_10_MINUTES", "7")
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "10.9.0.2")
    setup_request_windows.clear()
    auth._TRUSTED_PROXY_CACHE.clear()
    yield
    setup_request_windows.clear()


def _attempt(client, real_ip=None, code=b"wrong"):
    headers = {"x-vertep-setup-token": code.decode()}
    if real_ip:
        headers["X-Real-IP"] = real_ip
    return client.get("/api/setup/status", headers=headers).status_code


def test_one_client_behind_the_proxy_no_longer_locks_out_the_others(setup_mode):
    proxy = _client_from("10.9.0.2")
    assert [_attempt(proxy, "198.51.100.7") for _ in range(4)] == [401, 401, 401, 429]
    assert _attempt(proxy, "203.0.113.9") == 401  # another browser is not locked
    assert _attempt(proxy, "203.0.113.9", SETUP_CODE) != 401


def test_forged_real_ip_from_an_untrusted_peer_is_ignored(setup_mode):
    direct = _client_from("172.16.5.5")
    statuses = [_attempt(direct, f"192.0.2.{index}") for index in range(5)]
    assert statuses == [401, 401, 401, 429, 429]  # rotating the header does not escape the limit


def test_installation_wide_ceiling_still_protects_the_setup_code(setup_mode):
    proxy = _client_from("10.9.0.2")
    statuses = [_attempt(proxy, f"198.51.100.{index}") for index in range(8)]
    assert statuses == [401] * 7 + [429]


def test_invalid_real_ip_falls_back_to_the_peer(setup_mode):
    proxy = _client_from("10.9.0.2")
    assert [_attempt(proxy, "not-an-ip") for _ in range(4)] == [401, 401, 401, 429]


def test_expiry_guard_is_unchanged(setup_mode, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    monkeypatch.setenv("SETUP_TOKEN_EXPIRES_AT", past)
    assert _attempt(_client_from("10.9.0.2"), "198.51.100.7", SETUP_CODE) == 410


def test_cidr_trusted_proxy_and_unresolvable_host(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "10.9.0.0/24,host-that-does-not-resolve.invalid")
    auth._TRUSTED_PROXY_CACHE.clear()
    assert auth._is_trusted_proxy("10.9.0.77") is True
    assert auth._is_trusted_proxy("10.9.1.1") is False
    assert auth._is_trusted_proxy("testclient") is False


def test_shipped_proxy_overwrites_real_ip_and_forwarded_proto():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("deploy/proxy.conf", "deploy/proxy.conf.template"):
        text = (root / name).read_text(encoding="utf-8")
        # Every proxied location (CORE and, since #504, the web-ui panel) overwrites both.
        locations = text.count("proxy_pass ")
        assert locations == 3
        assert text.count("proxy_set_header X-Real-IP $remote_addr;") == locations
        assert text.count("proxy_set_header X-Forwarded-Proto https;") == locations
