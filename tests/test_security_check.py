"""Security check effective-state contract (Issue #84).

``/api/security/check`` must reflect the *effective* configuration, not just
file prefixes or env presence.  A missing certificate, an unreadable key or an
unsealed secret store must never be folded into the "ok" bucket.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api.observability import _certificate_statuses, _secret_store_status, _env_weak_values, security_check
from core.first_run import config_root, ensure_secret_store


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("CORE_CERTIFICATE_PATH", str(tmp_path / "vertep.crt"))
    monkeypatch.setenv("CORE_KEY_PATH", str(tmp_path / "vertep.key"))
    monkeypatch.setenv("NODE_CA_CERT_PATH", str(tmp_path / "node-ca.crt"))
    monkeypatch.setenv("ADMIN_PASSWORD", "a" * 32)
    monkeypatch.setenv("NODE_API_TOKEN", "b" * 32)
    monkeypatch.setenv("POSTGRES_PASSWORD", "c" * 32)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "d" * 32)
    monkeypatch.setenv("SESSION_SECRET", "e" * 32)
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    ensure_secret_store()
    yield security_check


def reseal(tmp_path: Path) -> None:
    """Regenerate the store under a fresh data key sealed with the current passphrase."""
    for name in ("secret-store.key", "secrets.enc.json"):
        (tmp_path / name).unlink(missing_ok=True)
    ensure_secret_store()


def test_missing_certificates_fail_security_check(client, tmp_path):
    """Issue #84: a missing cert must not be reported as ok."""
    body = client()
    assert body["ok"] is False
    assert body["checks"]["certificates"]["server_certificate"]["status"] == "missing"
    assert body["checks"]["certificates"]["server_key"]["status"] == "missing"
    assert body["checks"]["certificates"]["node_ca"]["status"] == "missing"


def test_unreadable_key_fails_security_check(client, tmp_path):
    (tmp_path / "vertep.crt").write_bytes(b"not-a-cert")
    body = client()
    assert body["ok"] is False
    assert body["checks"]["certificates"]["server_certificate"]["status"] == "unreadable"


def test_unsealed_store_warns_when_passphrase_configured(client, tmp_path):
    """Issue #84: an unsealed store is a warning, not an automatic failure,
    when the operator has configured a passphrase but not yet re-sealed."""
    (tmp_path / "secret-store.key").write_text("plain-text-key", encoding="utf-8")
    body = client()
    assert body["checks"]["secrets_store"]["sealed"] is False
    assert body["checks"]["secrets_store"]["status"] == "warning"
    assert "Re-seal" in body["recommendation"]


def test_sealed_store_is_reported_sealed_and_ok(client, tmp_path):
    """A real scrypt+A256GCM envelope must be recognised as sealed even though
    it has no ``wrapped_data_key`` field."""
    reseal(tmp_path)
    body = _secret_store_status()
    assert body["sealed"] is True
    assert body["unsealable"] is True
    assert body["status"] == "ok"


def test_sealed_store_with_wrong_passphrase_fails_closed(client, tmp_path):
    """Issue #84: sealing is effective state. A sealed key that Core cannot
    open with the configured passphrase must fail the gate, not report ok."""
    reseal(tmp_path)
    os.environ["SECRET_STORE_PASSPHRASE"] = "f" * 32
    body = client()
    checks = body["checks"]["secrets_store"]
    assert checks["sealed"] is True
    assert checks["unsealable"] is False
    assert checks["status"] == "unusable"
    assert body["ok"] is False
    assert "cannot be opened" in body["recommendation"]


def test_sealed_store_with_missing_passphrase_fails_closed(client, tmp_path):
    reseal(tmp_path)
    os.environ.pop("SECRET_STORE_PASSPHRASE", None)
    os.environ.pop("SECRET_STORE_PASSPHRASE_FILE", None)
    body = client()
    checks = body["checks"]["secrets_store"]
    assert checks["sealed"] is True
    assert checks["unsealable"] is False
    assert checks["status"] == "unusable"
    assert body["ok"] is False


def test_missing_secret_store_passphrase_keeps_ok_when_not_required(client, tmp_path):
    os.environ.pop("SECRET_STORE_PASSPHRASE", None)
    os.environ.pop("SECRET_STORE_PASSPHRASE_FILE", None)
    (tmp_path / "secret-store.key").unlink(missing_ok=True)
    body = client()
    # No data key on disk yet and no passphrase configured: the store is simply
    # absent, which is the initial-state baseline and not itself a violation.
    # The check still fails here because the certificates are also missing.
    assert body["checks"]["secrets_store"]["sealed"] is None
    assert body["checks"]["secrets_store"]["status"] == "ok"


def test_certificate_statuses_include_sha256_and_subject(client, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as _dt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vertep-test")])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc))
            .not_valid_after(_dt.datetime(2030, 1, 1, tzinfo=_dt.timezone.utc))
            .sign(key, hashes.SHA256()))
    (tmp_path / "vertep.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "vertep.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))

    statuses = _certificate_statuses()
    assert statuses["server_certificate"]["status"] == "ok"
    assert "sha256" in statuses["server_certificate"]
    assert "subject" in statuses["server_certificate"]
    assert statuses["server_key"]["status"] == "ok"


def test_weak_env_value_fails_security_check(client, tmp_path):
    os.environ["ADMIN_PASSWORD"] = "changeme"
    body = client()
    assert body["ok"] is False
    assert "ADMIN_PASSWORD" in body["weak_or_missing"]


def test_certificate_expiry_parsing_is_fail_closed():
    """Issue #84: a missing or unparseable node certificate expiry must never
    keep a stale certificate trusted. Pure parsing, so no openssl is needed."""
    from core.node_registry import _certificate_expired

    assert _certificate_expired("Aug 20 12:00:00 2020 GMT") is True
    assert _certificate_expired("Aug 20 12:00:00 2999 GMT") is False
    assert _certificate_expired("2999-01-01T00:00:00+00:00") is False
    assert _certificate_expired("2000-01-01T00:00:00+00:00") is True
    assert _certificate_expired("not-a-date") is True
    assert _certificate_expired("") is True
    assert _certificate_expired(None) is True