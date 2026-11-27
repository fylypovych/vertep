"""Connecting the LAB to this CORE (#491): one-time token instead of manual settings.

The administrator creates a short-lived one-time token in Vertep. The LAB
bootstrap pins CORE's web certificate (fingerprint shown with the token), sends
the token and a CSR for its panel; CORE signs the panel certificate with its own
LAB-panel CA — never the node CA that nodes trust for CORE — (the LAB key never
leaves the LAB), creates the GPU-lease credential, remembers the panel address
and answers once. The LAB never gets a
production node role, module or other secret through this exchange.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_LIFETIME = timedelta(hours=1)
PANEL_CERTIFICATE_DAYS = 397
_FILE = "lab-connection.json"
_lock = threading.Lock()


class LabConnectionError(ValueError):
    pass


def _path() -> Path:
    from .first_run import config_root
    return config_root() / _FILE


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def core_certificate_sha256() -> str:
    """Fingerprint of the web certificate the LAB must see when it connects (pinned)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    path = Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/tls/vertep.crt"))
    return x509.load_pem_x509_certificate(path.read_bytes()).fingerprint(hashes.SHA256()).hex()


def issue_token() -> dict:
    token = secrets.token_hex(32)
    expires = _now() + TOKEN_LIFETIME
    with _lock:
        data = _load()
        # One open token at a time: a new one replaces the previous, unused one.
        data["pending"] = {"sha256": _digest(token), "expires_at": expires.isoformat()}
        _save(data)
    return {"token": token, "expires_at": expires.isoformat(), "core_certificate_sha256": core_certificate_sha256()}


def status() -> dict:
    data = _load()
    return {"connected": bool(data.get("lease_token_sha256")), "connected_at": data.get("connected_at"),
            "panel_url": data.get("panel_url")}


def disconnect() -> None:
    with _lock:
        data = _load()
        for key in ("lease_token_sha256", "connected_at", "panel_url", "pending"):
            data.pop(key, None)
        _save(data)


def lease_token_valid(token: str) -> bool:
    expected = _load().get("lease_token_sha256")
    return bool(token and expected and secrets.compare_digest(_digest(token), expected))


def connected() -> bool:
    return bool(_load().get("lease_token_sha256"))


def _panel_name(panel: str):
    """The one name the panel certificate carries: the host of the panel address, chosen by
    CORE, never names from the request; CORE's own web names are refused."""
    import ipaddress
    from urllib.parse import urlsplit
    from cryptography import x509
    host = urlsplit(panel).hostname or ""
    try:
        name = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        name = x509.DNSName(host)
    try:
        web = x509.load_pem_x509_certificate(
            Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/tls/vertep.crt")).read_bytes())
        own = web.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        own = []
    if name in own:
        raise LabConnectionError("The panel address must not be CORE's own address")
    return name


def _lab_panel_ca() -> tuple[Path, Path]:
    """A CA only for LAB panels. Nodes are given the installation (node) CA to trust CORE,
    so a panel certificate signed by it could impersonate CORE to them; this one they never get."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from .first_run import config_root
    key_path, cert_path = config_root() / "pki/lab-panel-ca.key", config_root() / "pki/lab-panel-ca.crt"
    if key_path.is_file() and cert_path.is_file():
        return key_path, cert_path
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Vertep LAB Panel CA")])
    now = _now()
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(secrets.randbits(159) or 1)
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=False, key_encipherment=False,
                                                content_commitment=False, data_encipherment=False,
                                                key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .sign(key, hashes.SHA256()))
    key_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _sign_panel(csr_pem: str, panel: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from .first_run import config_root
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as error:
        raise LabConnectionError("The panel certificate request is not a PEM CSR") from error
    if not csr.is_signature_valid:
        raise LabConnectionError("The panel certificate request signature is invalid")
    name = _panel_name(panel)
    ca_key_path, ca_cert_path = _lab_panel_ca()
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    ca_certificate = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    now = _now()
    certificate = (x509.CertificateBuilder()
                   # Subject and names are CORE's choice; only the key comes from the request.
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Vertep LAB")]))
                   .issuer_name(ca_certificate.subject)
                   .public_key(csr.public_key())
                   .serial_number(secrets.randbits(159) or 1)
                   .not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=PANEL_CERTIFICATE_DAYS))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.SubjectAlternativeName([name]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM).decode(),
            ca_cert_path.read_text(encoding="utf-8"))


def _panel_url(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d{1,5})?|https://\[[0-9A-Fa-f:.]+\](:\d{1,5})?",
                                                        value.strip().rstrip("/")):
        raise LabConnectionError("The panel address must be https://host[:port]")
    return value.strip().rstrip("/")


def connect(token: str, csr_pem: str, panel_url: str) -> dict:
    """Consume the one-time token; answer with the panel certificate and the lease credential."""
    panel = _panel_url(panel_url)
    with _lock:
        data = _load()
        pending = data.get("pending") or {}
        try:
            expires = datetime.fromisoformat(pending.get("expires_at", ""))
        except ValueError:
            expires = None
        if (not isinstance(token, str) or not pending.get("sha256") or expires is None
                or not secrets.compare_digest(_digest(token), pending["sha256"])):
            raise PermissionError("Invalid LAB connection token")
        data.pop("pending")  # one use, even when the request below is rejected
        _save(data)
        if _now() >= expires:
            raise PermissionError("The LAB connection token has expired")
        certificate, ca = _sign_panel(csr_pem, panel)
        lease_token = secrets.token_hex(32)
        data.update(lease_token_sha256=_digest(lease_token), connected_at=_now().isoformat(), panel_url=panel)
        _save(data)
    from .lab_identity import set_panel_url
    set_panel_url(panel)
    return {"panel_certificate": certificate, "panel_ca": ca, "resource_token": lease_token}

