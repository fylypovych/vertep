"""The LAB reaches CORE with CORE's own self-signed certificate pinned at «Підключити
лабораторію» (#491). That certificate names vertep.local, not 10.10.10.10, so the LAB
answered 500 on /api/lab/status: for exactly that pinned leaf only the certificate is
checked, never switched off; a real CA keeps the host-name check."""

import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _certificate(tmp_path, name, ca=False, san="vertep.local"):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
               .not_valid_after(now + timedelta(days=1))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False))
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    certificate = builder.sign(key, hashes.SHA256())
    (tmp_path / f"{name}.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (tmp_path / f"{name}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                              serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()))
    return tmp_path / f"{name}.crt", tmp_path / f"{name}.key"


@pytest.fixture
def core(tmp_path):
    cert, key = _certificate(tmp_path, "core")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", cert
    server.shutdown()


def test_pinned_core_certificate_is_accepted_at_an_address_it_does_not_name(core, monkeypatch):
    from lab.tls import resource_context
    url, cert = core
    monkeypatch.setenv("LAB_RESOURCE_CA_FILE", str(cert))
    assert httpx.get(url, verify=resource_context()).text == "ok"


def test_any_other_certificate_is_still_refused(core, tmp_path, monkeypatch):
    from lab.tls import resource_context
    url, _ = core
    other, _ = _certificate(tmp_path, "other")
    monkeypatch.setenv("LAB_RESOURCE_CA_FILE", str(other))
    context = resource_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(httpx.ConnectError):
        httpx.get(url, verify=context)


def test_a_real_ca_keeps_the_host_name_check(tmp_path, monkeypatch):
    from lab.tls import resource_context
    ca, _ = _certificate(tmp_path, "ca", ca=True, san="Vertep CA")
    monkeypatch.setenv("LAB_RESOURCE_CA_FILE", str(ca))
    assert resource_context().check_hostname is True
    monkeypatch.delenv("LAB_RESOURCE_CA_FILE")
    assert resource_context().check_hostname is True
