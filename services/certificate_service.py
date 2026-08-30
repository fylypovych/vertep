"""Internal certificate lifecycle service used by the Zero-Shell UI."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException


app = FastAPI(title="Vertep Certificate Manager", version="1")


def _paths() -> tuple[Path, Path]:
    root = Path(os.getenv("TLS_ROOT", "/data/tls"))
    return root / "vertep.crt", root / "vertep.key"


def _details() -> dict:
    certificate_path, _ = _paths()
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    except (OSError, ValueError) as error:
        raise HTTPException(503, "TLS certificate is unavailable or invalid") from error
    return {"status": "HEALTHY", "subject": certificate.subject.rfc4514_string(),
            "issuer": certificate.issuer.rfc4514_string(),
            "not_before": certificate.not_valid_before_utc.isoformat(),
            "not_after": certificate.not_valid_after_utc.isoformat(),
            "sha256": certificate.fingerprint(hashes.SHA256()).hex()}


@app.get("/health")
def health() -> dict:
    return _details()


@app.get("/certificate")
def certificate() -> dict:
    return _details()


@app.post("/certificate/renew")
def renew() -> dict:
    certificate_path, key_path = _paths()
    certificate_path.parent.mkdir(parents=True, exist_ok=True)
    common_name = os.getenv("WEB_DOMAIN", "vertep.local").strip() or "vertep.local"
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)))
    try:
        san = x509.IPAddress(ip_address(common_name))
    except ValueError:
        san = x509.DNSName(common_name)
    certificate = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False).sign(key, hashes.SHA256())
    encoded_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    encoded_certificate = certificate.public_bytes(serialization.Encoding.PEM)
    for destination, data in ((key_path, encoded_key), (certificate_path, encoded_certificate)):
        handle, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600 if destination == key_path else 0o644)
            os.replace(temporary_name, destination)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    return _details()
