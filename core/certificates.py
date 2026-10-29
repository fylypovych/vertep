"""Local certificate inventory used by status and Full Self-Test."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding


def certificate_execution_probes() -> list[dict]:
    """Verify configured certificate/key pairs by signing a fresh challenge."""
    from .first_run import config_root
    keys = {"server_certificate": Path(os.getenv("CORE_KEY_PATH", "/data/tls/vertep.key")),
            "node_ca": Path(os.getenv("NODE_CA_KEY_PATH", str(config_root() / "pki/ca.key")))}
    result = []
    for record in list_certificates():
        try:
            certificate = x509.load_pem_x509_certificate(Path(record["path"]).read_bytes())
            key = serialization.load_pem_private_key(keys[record["cert_id"]].read_bytes(), password=None)
            challenge = os.urandom(32)
            if isinstance(key, rsa.RSAPrivateKey):
                signature = key.sign(challenge, padding.PKCS1v15(), hashes.SHA256())
                certificate.public_key().verify(signature, challenge, padding.PKCS1v15(), hashes.SHA256())
            elif isinstance(key, ec.EllipticCurvePrivateKey):
                signature = key.sign(challenge, ec.ECDSA(hashes.SHA256()))
                certificate.public_key().verify(signature, challenge, ec.ECDSA(hashes.SHA256()))
            else:
                certificate.public_key().verify(key.sign(challenge), challenge)
            result.append({"cert_id": record["cert_id"], "sha256": record["sha256"], "signature_verified": True})
        except Exception:
            result.append({"cert_id": record["cert_id"], "signature_verified": False})
    return result


def list_certificates() -> list[dict]:
    """Return readable configured certificates with normalized expiry data."""
    now = datetime.now(timezone.utc)
    result: list[dict] = []
    from .first_run import config_root
    definitions = (
        ("server_certificate", "CORE_CERTIFICATE_PATH", "/data/tls/vertep.crt"),
        ("node_ca", "NODE_CA_CERT_PATH", str(config_root() / "pki/ca.crt")),
    )
    for certificate_id, environment, default in definitions:
        path = Path(os.getenv(environment, default))
        if not path.is_file():
            continue
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        not_after = getattr(certificate, "not_valid_after_utc", None)
        if not_after is None:
            not_after = certificate.not_valid_after.replace(tzinfo=timezone.utc)
        result.append({
            "cert_id": certificate_id,
            "path": str(path),
            "expires_at": not_after.isoformat(),
            "expires_in_days": (not_after - now).days,
            "sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        })
    return result
