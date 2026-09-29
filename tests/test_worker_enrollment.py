"""Disposable-enrollment lost-response retry (Issue #80).

The worker's single-use registration flow must not be lost to transient
transport failures.  This module exercises the retry wrapper around
``worker.service.enroll`` with a mock HTTP client so the retry policy is
verified without a live Core.
"""

import json
from pathlib import Path

import httpx
import pytest

from worker.service import _retry_enrollment


def _credentials(jwt="jwt-abc"):
    return {
        "jwt": jwt,
        "worker_secret": "secret",
        "certificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
        "core_certificate": "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----",
    }


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else _credentials()
        self.text = text or json.dumps(self._payload)
        self.request = None

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=self.request, response=self)


class _FakeClient:
    """Minimal stand-in for ``httpx.Client`` recording every attempt."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.requests = []

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        self.requests.append({"url": url, "json": json})
        if not self.statuses:
            return _FakeResponse()
        response = _FakeResponse(status_code=self.statuses.pop(0))
        if response.status_code >= 400:
            response.raise_for_status()
        return response


def test_enrollment_retries_transient_5xx_and_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_CONFIG_PATH", str(tmp_path / "node-credentials.json"))
    monkeypatch.setenv("REGISTRATION_TOKEN", "VT-AAAA-BBBB-CCCC")
    monkeypatch.setenv("CSR_PROVIDER", "-----BEGIN CERTIFICATE REQUEST-----\nstub\n-----END CERTIFICATE REQUEST-----")
    monkeypatch.setenv("ENROLLMENT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("ENROLLMENT_RETRY_BASE_SECONDS", "0")
    client = _FakeClient([503, 502, 200])
    jwt = _retry_enrollment(client, "https://core.example", "gpu-01", {}, ["image_generation"])
    assert jwt == "jwt-abc"
    assert client.calls == 3
    assert (tmp_path / "node-credentials.json").is_file()
    stored = json.loads((tmp_path / "node-credentials.json").read_text())
    assert stored["jwt"] == "jwt-abc"


def test_enrollment_gives_up_after_max_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_CONFIG_PATH", str(tmp_path / "node-credentials.json"))
    monkeypatch.setenv("REGISTRATION_TOKEN", "VT-AAAA-BBBB-CCCC")
    monkeypatch.setenv("CSR_PROVIDER", "-----BEGIN CERTIFICATE REQUEST-----\nstub\n-----END CERTIFICATE REQUEST-----")
    monkeypatch.setenv("ENROLLMENT_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("ENROLLMENT_RETRY_BASE_SECONDS", "0")
    client = _FakeClient([503, 503])
    with pytest.raises(httpx.HTTPStatusError):
        _retry_enrollment(client, "https://core.example", "gpu-01", {}, ["image_generation"])
    assert client.calls == 2
    assert not (tmp_path / "node-credentials.json").exists()


def test_enrollment_does_not_retry_permanent_4xx(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_CONFIG_PATH", str(tmp_path / "node-credentials.json"))
    monkeypatch.setenv("REGISTRATION_TOKEN", "VT-AAAA-BBBB-CCCC")
    monkeypatch.setenv("CSR_PROVIDER", "-----BEGIN CERTIFICATE REQUEST-----\nstub\n-----END CERTIFICATE REQUEST-----")
    monkeypatch.setenv("ENROLLMENT_RETRY_ATTEMPTS", "5")
    monkeypatch.setenv("ENROLLMENT_RETRY_BASE_SECONDS", "0")
    client = _FakeClient([401])
    with pytest.raises(httpx.HTTPStatusError):
        _retry_enrollment(client, "https://core.example", "gpu-01", {}, ["image_generation"])
    assert client.calls == 1


def test_enrollment_retries_connection_failure(monkeypatch, tmp_path):
    """A lost response (no HTTP reply at all) is retried, unlike a 4xx."""
    monkeypatch.setenv("NODE_CONFIG_PATH", str(tmp_path / "node-credentials.json"))
    monkeypatch.setenv("REGISTRATION_TOKEN", "VT-AAAA-BBBB-CCCC")
    monkeypatch.setenv("CSR_PROVIDER", "-----BEGIN CERTIFICATE REQUEST-----\nstub\n-----END CERTIFICATE REQUEST-----")
    monkeypatch.setenv("ENROLLMENT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("ENROLLMENT_RETRY_BASE_SECONDS", "0")

    attempts = {"count": 0}

    class _FailingClient:
        def post(self, url, json=None, timeout=None):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise httpx.ConnectError("connection reset")
            return _FakeResponse()

    jwt = _retry_enrollment(_FailingClient(), "https://core.example", "gpu-01", {}, ["image_generation"])
    assert jwt == "jwt-abc"
    assert attempts["count"] == 3