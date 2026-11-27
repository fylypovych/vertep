"""#491: the LAB connects itself to CORE with a one-time token — no manual certificate,
lease token, Vertep address or CA copying.

CORE side: the token is one-use and short-lived, the panel certificate is signed by the
installation CA with CORE's own subject and server-only usage, the lease credential it
returns is accepted by the GPU-lease API and revoked by «Відключити».
LAB side: the bootstrap's connection step really runs against an HTTPS endpoint — it sends
nothing when CORE's certificate does not match the pin, and otherwise writes the panel key
(generated locally), the certificate chain and CORE's certificate.
"""

import json
import re
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).parents[1]


def _self_signed(tmp_path, name="core"):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, certificate.fingerprint(hashes.SHA256()).hex()


@pytest.fixture
def core(monkeypatch, tmp_path):
    from core import node_registry
    config = tmp_path / "config"
    (config / "pki").mkdir(parents=True)
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("NODE_CA_KEY_PATH", str(config / "pki/ca.key"))
    monkeypatch.setenv("NODE_CA_CERT_PATH", str(config / "pki/ca.crt"))
    node_registry._create_installation_ca(config / "pki/ca.key", config / "pki/ca.crt")
    web_cert, _, fingerprint = _self_signed(tmp_path)
    monkeypatch.setenv("CORE_CERTIFICATE_PATH", str(web_cert))
    from core import lab_connection
    return lab_connection, fingerprint, config


def _csr(*names):
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "evil.example")]))
               .add_extension(x509.SubjectAlternativeName(list(names)), critical=False))
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def test_one_time_token_signs_a_server_only_panel_certificate_and_grants_the_lease(core):
    lab_connection, fingerprint, config = core
    issued = lab_connection.issue_token()
    assert issued["core_certificate_sha256"] == fingerprint
    assert issued["token"] not in (config / "lab-connection.json").read_text()  # stored hashed
    answer = lab_connection.connect(issued["token"], _csr(x509.DNSName("lab"), x509.DNSName("core.example"),
                                   x509.UniformResourceIdentifier("spiffe://vertep/node/x")), "https://10.10.10.50:8444")
    certificate = x509.load_pem_x509_certificate(answer["panel_certificate"].encode())
    ca = x509.load_pem_x509_certificate(answer["panel_ca"].encode())
    assert certificate.issuer == ca.subject
    node_ca = x509.load_pem_x509_certificate((config / "pki/ca.crt").read_bytes())
    assert certificate.issuer != node_ca.subject  # nodes trust the node CA for CORE: never sign panels with it
    certificate.verify_directly_issued_by(ca)
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "Vertep LAB"  # not the CSR's
    assert certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.SERVER_AUTH])
    assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    # Only the panel host, whatever names the request asked for.
    assert [str(n.value) for n in certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value] == ["10.10.10.50"]
    assert lab_connection.lease_token_valid(answer["resource_token"]) and lab_connection.connected()
    assert lab_connection.status()["panel_url"] == "https://10.10.10.50:8444"
    from core.lab_identity import panel_url
    assert panel_url() == "https://10.10.10.50:8444"
    with pytest.raises(PermissionError):  # one use only
        lab_connection.connect(issued["token"], _csr(x509.DNSName("lab")), "https://10.10.10.50:8444")
    lab_connection.disconnect()
    assert not lab_connection.lease_token_valid(answer["resource_token"]) and not lab_connection.connected()


def test_expired_wrong_or_unsafe_requests_are_refused_and_burn_the_token(core, monkeypatch):
    lab_connection = core[0]
    with pytest.raises(PermissionError):
        lab_connection.connect("guess", _csr(x509.DNSName("lab")), "https://lab:8444")
    issued = lab_connection.issue_token()
    with pytest.raises(lab_connection.LabConnectionError):  # CORE's own web name (fixture: localhost)
        lab_connection.connect(issued["token"], _csr(x509.DNSName("lab")), "https://localhost:8444")
    with pytest.raises(PermissionError):  # the rejected attempt used the token up
        lab_connection.connect(issued["token"], _csr(x509.DNSName("lab")), "https://lab:8444")
    issued = lab_connection.issue_token()
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(lab_connection, "_now", lambda: later)
    with pytest.raises(PermissionError, match="expired"):
        lab_connection.connect(issued["token"], _csr(x509.DNSName("lab")), "https://lab:8444")
    assert not lab_connection.connected()


def test_lease_api_accepts_the_connection_credential(core, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.api import lab_lease
    lab_connection = core[0]
    monkeypatch.delenv("LAB_LEASE_TOKEN", raising=False)
    monkeypatch.delenv("LAB_IDLE_SHARING_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(lab_lease.router)
    client = TestClient(app)
    assert client.get("/lab-lease/v1/identity-key", headers={"x-vertep-lab-token": "x"}).status_code == 401
    token = lab_connection.connect(lab_connection.issue_token()["token"], _csr(x509.DNSName("lab")),
                                   "https://lab:8444")["resource_token"]
    response = client.get("/lab-lease/v1/identity-key", headers={"x-vertep-lab-token": token})
    assert response.status_code == 200 and response.json()["algorithm"] == "Ed25519"
    assert lab_lease._enabled()


def _bootstrap_connector() -> str:
    text = (ROOT / "bootstrap-lab.sh").read_text(encoding="utf-8")
    return re.search(r"<<'PY'\n(import hashlib, http\.client.*?)\nPY\n", text, re.S).group(1)


@pytest.fixture
def fake_core(tmp_path):
    cert, key, fingerprint = _self_signed(tmp_path, "fake-core")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"panel_certificate": "CERT\n", "panel_ca": "CA\n", "resource_token": "lease-1"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", fingerprint, received
    server.shutdown()


def _run_connector(tmp_path, url, pin):
    (tmp_path / "tls").mkdir(exist_ok=True)
    (tmp_path / "ca").mkdir(exist_ok=True)
    return subprocess.run([sys.executable, "-I", "-", url, pin, "https://10.10.10.50:8444", str(tmp_path / "tls"),
                           str(tmp_path / "ca/vertep-core.pem"), "lab.local", "10.10.10.50", "10.10.10.50"],
                          input=_bootstrap_connector(), capture_output=True, text=True, timeout=60,
                          env={"VERTEP_LAB_TOKEN": "one-time", "PATH": "/usr/bin:/bin"})


def test_bootstrap_sends_nothing_when_the_core_certificate_does_not_match_the_pin(fake_core, tmp_path):
    url, _, received = fake_core
    result = _run_connector(tmp_path, url, "00" * 32)
    assert result.returncode != 0 and "does not match" in result.stderr
    assert received == [] and not (tmp_path / "tls/lab.key").exists()


def test_bootstrap_connects_with_a_local_key_and_writes_the_panel_chain(fake_core, tmp_path):
    url, fingerprint, received = fake_core
    result = _run_connector(tmp_path, url, fingerprint)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "lease-1"
    path, body = received[0]
    assert path == "/api/lab/connect" and body["token"] == "one-time" and body["panel_url"] == "https://10.10.10.50:8444"
    csr = x509.load_pem_x509_csr(body["csr"].encode())
    names = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(item.value) for item in names] == ["lab.local", "10.10.10.50"]  # duplicates dropped
    key = serialization.load_pem_private_key((tmp_path / "tls/lab.key").read_bytes(), None)
    assert key.public_key().public_numbers() == csr.public_key().public_numbers()  # key made here, never sent
    assert (tmp_path / "tls/lab.key").stat().st_mode & 0o077 == 0
    assert (tmp_path / "tls/lab.crt").read_text() == "CERT\nCA\n"
    pinned = x509.load_pem_x509_certificate((tmp_path / "ca/vertep-core.pem").read_bytes())
    assert pinned.fingerprint(hashes.SHA256()).hex() == fingerprint


def test_bootstrap_points_the_lab_at_vertep_and_no_longer_asks_to_copy_certificates():
    text = (ROOT / "bootstrap-lab.sh").read_text(encoding="utf-8")
    for line in ("set_value LAB_VERTEP_URL", "set_value LAB_RESOURCE_URL \"$core_url/lab-lease/v1\"",
                 "set_value LAB_RESOURCE_TOKEN", "set_value LAB_RESOURCE_CA_FILE /run/lab-ca/vertep-core.pem"):
        assert line in text
    assert "замініть lab.crt" not in text
    assert "$owner_token" not in text  # the owner token stays in lab.env, not on the console


def test_only_the_connect_call_skips_the_vertep_session(core, monkeypatch):
    from fastapi.testclient import TestClient
    from core.api import auth as auth_module
    from core.app import app
    monkeypatch.setattr(auth_module, "is_configured", lambda: True)  # an installed CORE
    client = TestClient(app, raise_server_exceptions=False)  # the suite's admin credentials
    stranger = TestClient(app, raise_server_exceptions=False, headers={"Authorization": "Basic eDp5"})
    anonymous = stranger.post("/api/lab/connect", json={"token": "guess", "csr": "", "panel_url": "https://lab:8444"})
    assert anonymous.status_code == 401 and anonymous.json()["detail"] == "Invalid LAB connection token"
    for method, path in (("POST", "/api/lab/connection-token"), ("GET", "/api/lab/connection"),
                         ("DELETE", "/api/lab/connection")):
        assert stranger.request(method, path).status_code in (401, 403), path
    issued = client.post("/api/lab/connection-token")
    assert issued.status_code == 200 and len(issued.json()["token"]) == 64
    connected = client.post("/api/lab/connect", json={"token": issued.json()["token"], "csr": _csr(x509.DNSName("lab")),
                                                      "panel_url": "https://lab:8444"})
    assert connected.status_code == 200 and connected.json()["resource_token"]
    assert client.get("/api/lab/connection").json()["connected"] is True
    assert client.delete("/api/lab/connection").json() == {"connected": False}
