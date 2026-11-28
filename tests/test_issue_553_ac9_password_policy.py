"""#553 AC9: align First Run password policy and /api/security/check diagnostics.

A password accepted by the First Run policy must not be flagged as weak by
``/api/security/check``, and any password flagged as weak must be rejected by
First Run. Appliance deployments with a First Run administrator (without env
``ADMIN_PASSWORD``) and a file-backed ``SECRET_STORE_PASSPHRASE_FILE`` must
not produce false positives in ``weak_or_missing``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from core.api.observability import security_check
from core.app import app
from core.first_run import complete_setup, ensure_secret_store, password_hash
from core.security import PASSWORD_MIN_LENGTH, password_policy, password_policy_violations


def _write_valid_tls_bundle(directory: Path) -> tuple[Path, Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vertep.local")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("vertep.local")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    server_cert = directory / "vertep.crt"
    server_key = directory / "vertep.key"
    node_ca = directory / "node-ca.crt"
    server_cert.write_bytes(cert_pem)
    server_key.write_bytes(key_pem)
    node_ca.write_bytes(cert_pem)
    return server_cert, server_key, node_ca


@pytest.fixture
def security_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    tls_dir = tmp_path / "tls"
    config_dir.mkdir()
    tls_dir.mkdir()
    server_cert, server_key, node_ca = _write_valid_tls_bundle(tls_dir)

    monkeypatch.setenv("CONFIG_ROOT", str(config_dir))
    monkeypatch.setenv("CORE_CERTIFICATE_PATH", str(server_cert))
    monkeypatch.setenv("CORE_KEY_PATH", str(server_key))
    monkeypatch.setenv("NODE_CA_CERT_PATH", str(node_ca))
    monkeypatch.setenv("NODE_API_TOKEN", "k9Qm2pL8xV4nR7wT3zB6yC1dF5hJ0sA2")
    monkeypatch.setenv("POSTGRES_PASSWORD", "m4Np8qW2vX6zL9rT1bH5kD7fG3jS0cY8")
    monkeypatch.setenv("SESSION_SECRET", "p7Rs3vY9wB2mN5kQ8xL4tH6dF1gJ0zC3")
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.delenv("USERS_JSON", raising=False)
    monkeypatch.delenv("SECRET_STORE_PASSPHRASE", raising=False)
    monkeypatch.delenv("SECRET_STORE_PASSPHRASE_FILE", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("REQUIRE_SECRET_KEY_SEALING", raising=False)
    return config_dir


def test_min_length_12_password_accepted_by_policy_passes_security_check(security_env: Path, monkeypatch):
    # Boundary value: exact min_length (12) without weak markers.
    pw = "Vrt9#kL2mQ8z"
    assert len(pw) == PASSWORD_MIN_LENGTH == password_policy()["min_length"] == 12
    assert password_policy_violations(pw) == []

    monkeypatch.setenv("ADMIN_PASSWORD", pw)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "z8Qx2vB6nM4kP9rT1wL5hD7fG3jS0cY2")
    ensure_secret_store()

    body = security_check()
    assert "ADMIN_PASSWORD" not in body["weak_or_missing"]
    assert body["weak_or_missing"] == []
    assert body["ok"] is True


@pytest.mark.parametrize(
    "weak_password",
    [
        "",
        "           ",
        "            ",  # 12 spaces — blank
        "Vrt9#kL2mQ8",  # 11 chars (min_length - 1)
        "changeme",
        "changeme-12345678",
        "Change-Me-98765432",
        "replace-this-admin-pass",
        "example-admin-secret-99",
        "<your-admin-password>",
        "todo-rotate-password-1",
        "my-test-admin-secret-1",
    ],
)
def test_weak_or_default_password_rejected_by_first_run_and_caught_by_security_check(
    security_env: Path, monkeypatch, weak_password: str
):
    assert password_policy_violations(weak_password), f"Policy must reject {weak_password!r}"
    with pytest.raises(ValueError):
        complete_setup("Appliance", "operator", weak_password, weak_password, "skip")

    monkeypatch.setenv("ADMIN_PASSWORD", weak_password)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "z8Qx2vB6nM4kP9rT1wL5hD7fG3jS0cY2")
    ensure_secret_store()

    body = security_check()
    assert "ADMIN_PASSWORD" in body["weak_or_missing"]
    assert body["ok"] is False


def test_first_run_configured_admin_without_env_password_passes_security_check(
    security_env: Path, monkeypatch
):
    pw = "Vrt9#kL2mQ8z"  # exact 12-char boundary password accepted by First Run
    passphrase_file = security_env / "secret-store.passphrase"
    passphrase_value = "q4Wz9xC2vB7nM1kP8rT5hL3dF6gJ0yA9"
    passphrase_file.write_text(passphrase_value + "\n", encoding="utf-8")
    os.chmod(passphrase_file, 0o600)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE_FILE", str(passphrase_file))
    monkeypatch.setenv("REQUIRE_SECRET_KEY_SEALING", "true")

    complete_setup("Vertep Appliance", "owner", pw, pw, "skip")

    # Verify via direct function and live HTTP endpoint using the First Run credentials.
    body = security_check()
    assert body["weak_or_missing"] == []
    assert body["ok"] is True
    assert body["checks"]["effective_config"]["admin_password_set"] is True
    assert body["checks"]["effective_config"]["admin_credentials"] == {
        "state": "configured",
        "sources": ["first_run"],
        "problems": [],
    }
    assert body["checks"]["secrets_store"]["sealed"] is True
    assert body["checks"]["secrets_store"]["unsealable"] is True
    assert body["checks"]["secrets_store"]["passphrase_configured"] is True
    assert body["checks"]["secrets_store"]["status"] == "ok"
    assert passphrase_value not in json.dumps(body)

    auth = "Basic " + base64.b64encode(f"owner:{pw}".encode("utf-8")).decode("ascii")
    resp = TestClient(app, headers={"Authorization": auth}).get("/api/security/check")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["weak_or_missing"] == []


def test_user_store_admin_without_env_password_is_not_flagged_missing(
    security_env: Path, monkeypatch
):
    (security_env / "users.json").write_text(
        json.dumps({"ops": {"password_hash": password_hash("Ops9#kL2mQ8z"), "role": "admin"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "z8Qx2vB6nM4kP9rT1wL5hD7fG3jS0cY2")
    ensure_secret_store()

    body = security_check()
    assert "ADMIN_PASSWORD" not in body["weak_or_missing"]
    assert body["ok"] is True


def test_missing_or_damaged_admin_without_env_is_flagged_in_weak_or_missing(
    security_env: Path, monkeypatch
):
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "z8Qx2vB6nM4kP9rT1wL5hD7fG3jS0cY2")
    ensure_secret_store()

    # Neither env ADMIN_PASSWORD nor installation/user_store admin exists.
    body = security_check()
    assert "ADMIN_PASSWORD" in body["weak_or_missing"]
    assert body["ok"] is False

    # Damaged installation.json administrator record without env fallback must still fail.
    (security_env / "installation.json").write_text(
        json.dumps({"completed_at": "2026-01-01T00:00:00Z", "administrator": {"username": "owner", "password_hash": "broken"}}),
        encoding="utf-8",
    )
    body = security_check()
    assert "ADMIN_PASSWORD" in body["weak_or_missing"]
    assert body["ok"] is False


def test_weak_env_admin_password_still_flagged_even_when_first_run_admin_exists(
    security_env: Path, monkeypatch
):
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE", "z8Qx2vB6nM4kP9rT1wL5hD7fG3jS0cY2")
    complete_setup("Vertep Appliance", "owner", "Vrt9#kL2mQ8z", "Vrt9#kL2mQ8z", "skip")

    # Because _authenticate_user still accepts ADMIN_PASSWORD as an env fallback,
    # a weak env override must still be reported by /api/security/check.
    monkeypatch.setenv("ADMIN_PASSWORD", "changeme")
    body = security_check()
    assert "ADMIN_PASSWORD" in body["weak_or_missing"]
    assert body["ok"] is False


def test_file_backed_secret_store_passphrase_variants(security_env: Path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "Vrt9#kL2mQ8z")
    passphrase_file = security_env / "secret-store.passphrase"

    # 1. Strong file-backed passphrase (no env var): passes security check.
    strong = "h8Lp3nV6xR2mK9wQ4zB7cF1dG5jS0yN3"
    passphrase_file.write_text(strong + "\n", encoding="utf-8")
    os.chmod(passphrase_file, 0o600)
    monkeypatch.setenv("SECRET_STORE_PASSPHRASE_FILE", str(passphrase_file))
    ensure_secret_store()

    body = security_check()
    assert "SECRET_STORE_PASSPHRASE" not in body["weak_or_missing"]
    assert body["checks"]["secrets_store"]["passphrase_configured"] is True
    assert body["checks"]["secrets_store"]["sealed"] is True
    assert body["checks"]["secrets_store"]["status"] == "ok"
    assert body["checks"]["effective_config"]["secret_store_passphrase_set"] is True
    assert body["ok"] is True
    assert strong not in json.dumps(body)

    # 2. Short file-backed passphrase (< 16 chars): flagged in weak_or_missing.
    passphrase_file.write_text("short-pass-12", encoding="utf-8")
    body = security_check()
    assert "SECRET_STORE_PASSPHRASE" in body["weak_or_missing"]
    assert body["ok"] is False

    # 3. Weak/placeholder file-backed passphrase (even if >= 16 chars): flagged.
    passphrase_file.write_text("changeme-passphrase-123456", encoding="utf-8")
    body = security_check()
    assert "SECRET_STORE_PASSPHRASE" in body["weak_or_missing"]
    assert body["ok"] is False

    # 4. Empty passphrase file: flagged and passphrase_configured is False.
    passphrase_file.write_text("   \n", encoding="utf-8")
    body = security_check()
    assert "SECRET_STORE_PASSPHRASE" in body["weak_or_missing"]
    assert body["checks"]["secrets_store"]["passphrase_configured"] is False
    assert body["checks"]["effective_config"]["secret_store_passphrase_set"] is False
    assert body["ok"] is False

    # 5. Missing passphrase file: flagged and passphrase_configured is False.
    passphrase_file.unlink()
    body = security_check()
    assert "SECRET_STORE_PASSPHRASE" in body["weak_or_missing"]
    assert body["checks"]["secrets_store"]["passphrase_configured"] is False
    assert body["ok"] is False

    # 6. Neither env nor file set: flagged.
    monkeypatch.delenv("SECRET_STORE_PASSPHRASE_FILE", raising=False)
    body = security_check()
    assert "SECRET_STORE_PASSPHRASE" in body["weak_or_missing"]
    assert body["ok"] is False
