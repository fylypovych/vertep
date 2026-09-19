"""Local certificate inventory used by status and Full Self-Test."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes


def list_certificates() -> list[dict]:
    """Return readable configured certificates with normalized expiry data."""
    now = datetime.now(timezone.utc)
    result: list[dict] = []
    definitions = (
        ("server_certificate", "CORE_CERTIFICATE_PATH", "/data/tls/vertep.crt"),
        ("node_ca", "NODE_CA_CERT_PATH", "/data/tls/node-ca.crt"),
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
