import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.node_registry import (create_registration_token, enroll_node, registered_nodes,
                                create_node_csr, record_self_test, renew_node, revoke_node,
                                verify_node_certificate, verify_node_token,
                                rotate_node_credentials)


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="node PKI integration tests require openssl"
)


def test_registration_token_is_one_time_and_issues_bound_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    assert token["token"].startswith("VT-")
    # A capability no role declares must be dropped at enrollment: the reported set
    # is intersected with the registration role. Which capabilities a role owns is a
    # catalog decision that changes (Issue #122 added `video_generation` and
    # `video_assembly` to `gpu`), so the filter is proven with a capability that is
    # outside every role instead of a catalog snapshot.
    enrolled = enroll_node(token["token"], "gpu-01",
                           ["image_generation", "quantum_ray_tracing"],
                           {"gpu": "Tesla P100", "vram_mb": 16384, "cuda": "12.6"}, "1.3.0", csr)
    assert enrolled["status"] == "READY"
    assert enrolled["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert enrolled["core_certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert "private_key" not in enrolled
    assert (tmp_path / "client-pki/node.key").is_file()
    certificate = tmp_path / "issued.crt"
    certificate.write_text(enrolled["certificate"])
    details = subprocess.run(["openssl", "x509", "-in", str(certificate), "-text", "-noout"],
                             check=True, capture_output=True, text=True).stdout
    assert "TLS Web Client Authentication" in details
    assert "spiffe://vertep/node/gpu-01" in details
    assert verify_node_token(enrolled["jwt"], "gpu-01")
    original_serial = registered_nodes()[0]["certificate_serial"]
    assert verify_node_certificate("gpu-01", original_serial)
    assert verify_node_certificate("gpu-01", "0x00" + original_serial.lower())
    assert not verify_node_certificate("gpu-01", "DEADBEEF")
    assert not verify_node_token(enrolled["jwt"], "gpu-02")
    assert verify_node_token(enrolled["worker_secret"], "gpu-01")
    with pytest.raises(PermissionError, match="already used"):
        enroll_node(token["token"], "gpu-02", [], {}, "1.3.0", create_node_csr("gpu-02", tmp_path / "pki2"))
    assert registered_nodes()[0]["capabilities"] == ["image_generation"]
    assert "secret_hash" not in registered_nodes()[0]
    assert token["token"] not in (tmp_path / "node-registry.json").read_text()
    renewed = renew_node("gpu-01", csr)
    renewed_serial = registered_nodes()[0]["certificate_serial"]
    assert renewed_serial != original_serial
    assert not verify_node_certificate("gpu-01", original_serial)
    assert verify_node_certificate("gpu-01", renewed_serial)
    assert renewed["jwt"] != enrolled["jwt"]
    assert not verify_node_token(enrolled["jwt"], "gpu-01")
    assert verify_node_token(renewed["jwt"], "gpu-01")
    crl = tmp_path / "node-ca.crl"
    crl_text = subprocess.run(["openssl", "crl", "-in", str(crl), "-text", "-noout"],
                              check=True, capture_output=True, text=True).stdout
    assert original_serial.lower().lstrip("0") in crl_text.lower().replace(":", "").lstrip("0")
    revoke_node("gpu-01")
    assert not verify_node_certificate("gpu-01", renewed_serial)
    assert not verify_node_token(renewed["jwt"], "gpu-01")
    assert not verify_node_token(renewed["worker_secret"], "gpu-01")
    crl_text = subprocess.run(["openssl", "crl", "-in", str(crl), "-text", "-noout"],
                              check=True, capture_output=True, text=True).stdout.lower().replace(":", "")
    assert renewed_serial.lower().lstrip("0") in crl_text


def test_expired_certificate_is_rejected_even_with_matching_serial(monkeypatch, tmp_path):
    """Issue #84: serial binding alone must not keep an expired certificate
    trusted once the stored expiry is in the past."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enrolled = enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr)
    serial = registered_nodes()[0]["certificate_serial"]
    assert verify_node_certificate("gpu-01", serial)

    registry_path = tmp_path / "node-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["nodes"]["gpu-01"]["certificate_expires_at"] = "Aug 20 12:00:00 2020 GMT"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    assert not verify_node_certificate("gpu-01", serial)


def test_issued_certificate_carries_san_and_expiry(monkeypatch, tmp_path):
    """Issue #84: the disposable PKI acceptance also requires the issued
    certificate to carry the node SPIFFE SAN and a bounded validity window."""
    from cryptography import x509

    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("text", 900)
    csr = create_node_csr("text-01", tmp_path / "client-pki")
    enrolled = enroll_node(token["token"], "text-01", ["text_generation"], {}, "1.5.0", csr)
    certificate = x509.load_pem_x509_certificate(enrolled["certificate"].encode())
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = list(san.get_values_for_type(x509.UniformResourceIdentifier))
    assert "spiffe://vertep/node/text-01" in uris

    not_after, not_before = certificate.not_valid_after_utc, certificate.not_valid_before_utc
    assert not_after > not_before
    assert (not_after - not_before).days <= 400
    assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_role_catalog_is_extensible_without_registry_changes(monkeypatch, tmp_path):
    config = tmp_path / "roles.json"
    config.write_text(json.dumps({"future": {"label": "Future Node", "capabilities": ["new_engine"],
                                               "modules": ["future_runtime", "update_agent"]}}))
    monkeypatch.setenv("NODE_ROLES_FILE", str(config))
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path / "state"))
    token = create_registration_token("future", 60)
    result = enroll_node(token["token"], "future-01", [], {}, "2.0.0",
                         create_node_csr("future-01", tmp_path / "future-pki"))
    assert result["configuration"]["capabilities"] == ["new_engine"]


def test_self_test_records_runtime_status(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enroll_node(token["token"], "gpu-01", ["image_generation"],
                {"gpu": "RTX 4090", "vram_mb": 24576}, "1.5.0", csr)
    assert registered_nodes()[0]["runtime_status"] == "PENDING_SELF_TEST"
    record = record_self_test("gpu-01", "passed", ["image_generation", "image_upscale"])
    assert record["runtime_status"] == "ONLINE"
    node = registered_nodes()[0]
    assert node["runtime_status"] == "ONLINE"
    assert node["self_test_capabilities"] == ["image_generation", "image_upscale"]
    failed = record_self_test("gpu-01", "failed", ["image_generation"])
    assert failed["runtime_status"] == "OFFLINE"
    assert registered_nodes()[0]["runtime_status"] == "OFFLINE"
    with pytest.raises(ValueError):
        record_self_test("gpu-01", "weird", [])
    with pytest.raises(KeyError):
        record_self_test("no-such-node", "passed", [])


def test_workers_listing_propagates_registry_runtime_status(monkeypatch, tmp_path):
    """Issue #80: the /api/workers listing must surface the durable runtime_status
    for every role, including nodes whose live heartbeat record is offline."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("NODE_ROLES_FILE", str(tmp_path / "roles.json"))
    roles = {
        "voice": {"label": "Voice Node", "capabilities": ["speech_synthesis"],
                  "modules": ["tts_runtime", "update_agent"],
                  "services": ["worker", "tts", "update-agent"]},
        "publisher": {"label": "Publisher Node", "capabilities": ["publishing"],
                      "modules": ["publisher_worker", "update_agent"],
                      "services": ["worker", "publisher-worker", "update-agent"]},
        "monitoring": {"label": "Monitoring Node", "capabilities": ["metrics", "logs"],
                       "modules": ["grafana", "prometheus"],
                       "services": ["worker", "monitoring", "grafana", "update-agent"]},
        "backup": {"label": "Backup Node", "capabilities": ["backup", "snapshot"],
                   "modules": ["backup_service", "update_agent"],
                   "services": ["worker", "backup-service", "update-agent"]},
        "text": {"label": "Text Node", "capabilities": ["text_generation"],
                 "modules": ["ollama", "update_agent"],
                 "services": ["worker", "ollama", "update-agent"]},
        "gpu": {"label": "GPU Node", "capabilities": ["image_generation"],
                "modules": ["worker", "comfyui", "update_agent"],
                "services": ["worker", "comfyui", "update-agent"]},
    }
    (tmp_path / "roles.json").write_text(json.dumps(roles), encoding="utf-8")

    from core.node_registry import _save, _load, record_self_test
    from core.api.workers import workers as workers_listing

    registry = _load()
    for role in roles:
        registry["nodes"][f"{role}-01"] = {
            "node_id": f"{role}-01", "role": role, "capabilities": roles[role]["capabilities"],
            "hardware": {}, "version": "1.0.0", "secret_hash": "x", "status": "READY",
            "runtime_status": "PENDING_SELF_TEST", "self_test_capabilities": [],
            "last_self_test_at": None, "credential_generation": 1,
            "certificate_serial": f"0X{role.upper()}01", "certificate_expires_at": "2030-01-01T00:00:00+00:00",
            "registered_at": "2026-09-29T00:00:00+00:00"}
    _save(registry)

    listed = {item.get("node_id") or item.get("node_name"): item for item in workers_listing()}
    for role in roles:
        assert listed[f"{role}-01"]["runtime_status"] == "PENDING_SELF_TEST"

    record_self_test("voice-01", "passed", ["speech_synthesis"])
    record_self_test("publisher-01", "passed", ["publishing"])
    record_self_test("monitoring-01", "passed", ["metrics"])
    record_self_test("backup-01", "passed", ["backup"])
    record_self_test("text-01", "passed", ["text_generation"])
    record_self_test("gpu-01", "passed", ["image_generation"])
    listed = {item.get("node_id") or item.get("node_name"): item for item in workers_listing()}
    for role in roles:
        assert listed[f"{role}-01"]["runtime_status"] == "ONLINE"


def test_outbound_only_renew_and_revoke_roundtrip(monkeypatch, tmp_path):
    """Issue #80: renew and revoke must be usable as outbound-only operations
    from the node side, with the old certificate serial landing in the CRL."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enrolled = enroll_node(token["token"], "gpu-01", ["image_generation"],
                           {"gpu": "RTX 4090", "vram_mb": 24576}, "1.5.0", csr)
    original_serial = registered_nodes()[0]["certificate_serial"]

    renewed = renew_node("gpu-01", csr)
    assert renewed["certificate"] != enrolled["certificate"]
    assert renewed["jwt"] != enrolled["jwt"]
    assert registered_nodes()[0]["certificate_serial"] != original_serial
    assert not verify_node_certificate("gpu-01", original_serial)
    assert verify_node_certificate("gpu-01", renewed["certificate_serial"])

    revoked = revoke_node("gpu-01")
    assert revoked["status"] == "REVOKED"
    assert not verify_node_certificate("gpu-01", renewed["certificate_serial"])
    assert not verify_node_token(renewed["jwt"], "gpu-01")
    assert not verify_node_token(renewed["worker_secret"], "gpu-01")
    crl_text = subprocess.run(["openssl", "crl", "-in", str(tmp_path / "node-ca.crl"),
                               "-text", "-noout"], check=True,
                               capture_output=True, text=True).stdout.lower().replace(":", "")
    assert renewed["certificate_serial"].lower().lstrip("0") in crl_text


def test_lost_response_retry_reissues_credentials_for_same_node(monkeypatch, tmp_path):
    """Issue #80: a node that committed its enrollment but lost the response must be
    able to retry with the same enrollment id instead of deadlocking on a burned token."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    first = enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr,
                        enrollment_id="enroll-abc123")

    second = enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr,
                         enrollment_id="enroll-abc123")
    assert second["worker_secret"] != first["worker_secret"]
    assert second["jwt"] != first["jwt"]
    assert second["certificate_serial"] != first["certificate_serial"]
    assert verify_node_token(second["worker_secret"], "gpu-01")
    assert not verify_node_token(first["worker_secret"], "gpu-01")
    assert verify_node_certificate("gpu-01", second["certificate_serial"])
    assert not verify_node_certificate("gpu-01", first["certificate_serial"])
    assert registered_nodes()[0]["runtime_status"] == "PENDING_SELF_TEST"
    # The enrollment key is stored only as an HMAC.
    assert "enroll-abc123" not in (tmp_path / "node-registry.json").read_text(encoding="utf-8")


def test_reenrolling_a_known_node_revokes_its_previous_certificate(monkeypatch, tmp_path):
    """A node re-enrolled with a fresh token must lose the certificate it held."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    first_token = create_registration_token("gpu", 900)
    first = enroll_node(first_token["token"], "gpu-01", ["image_generation"], {}, "1.5.0",
                        create_node_csr("gpu-01", tmp_path / "client-pki"))
    assert verify_node_certificate("gpu-01", first["certificate_serial"])

    second_token = create_registration_token("gpu", 900)
    second = enroll_node(second_token["token"], "gpu-01", ["image_generation"], {}, "1.5.0",
                         create_node_csr("gpu-01", tmp_path / "client-pki"))
    assert second["certificate_serial"] != first["certificate_serial"]
    assert verify_node_certificate("gpu-01", second["certificate_serial"])
    assert not verify_node_certificate("gpu-01", first["certificate_serial"])
    from cryptography import x509
    crl = x509.load_pem_x509_crl((tmp_path / "node-ca.crl").read_bytes())
    # CRL serials are rendered as bare hex, so a stored serial with a leading
    # zero ("0A18…") must be compared in the same normalised form.
    def _normalise(serial: str) -> str:
        return serial.upper().lstrip("0") or "0"

    revoked = {_normalise(format(entry.serial_number, "X")) for entry in crl}
    assert _normalise(first["certificate_serial"]) in revoked
    assert _normalise(second["certificate_serial"]) not in revoked


def test_postgres_rotation_revokes_the_outgoing_certificate(monkeypatch, tmp_path):
    """On the PostgreSQL backend the rotation must still CRL the old certificate.

    The rotation clears ``certificate_serial`` in the same statement, so the value
    has to be read before the UPDATE; reading it back afterwards would silently
    leave the previous certificate trusted.
    """
    from core import node_registry

    # Materialise a real CA so the CRL rewrite after rotation is exercised.
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr)

    statements = []

    class Cursor:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=()):
            statements.append((" ".join(str(sql).split()), params))
            return self

        def fetchone(self):
            if "SELECT certificate_serial FROM registered_nodes" in statements[-1][0]:
                return {"certificate_serial": "0AABBCC"}
            if "UPDATE registered_nodes SET secret_hash=''" in statements[-1][0]:
                return {"node_id": "gpu-01", "role": "gpu", "capabilities": ["image_generation"],
                        "hardware": {}, "version": "1.5.0", "status": "READY",
                        "credential_generation": 2, "certificate_serial": None,
                        "certificate_expires_at": None, "runtime_status": "PENDING_SELF_TEST",
                        "last_self_test_at": None, "registered_at": "2026-09-29T00:00:00+00:00",
                        "revoked_at": None}
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def cursor(self, row_factory=None):
            return Cursor(self)

        def execute(self, sql, params=()):
            statements.append((" ".join(str(sql).split()), params))
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            if "FROM node_revoked_certificates" in statements[-1][0]:
                return [("0AABBCC", "2026-09-30T00:00:00+00:00")]
            return []

    psycopg = SimpleNamespace(connect=lambda *a, **k: Connection())
    rows = SimpleNamespace(dict_row=lambda row: row)
    psycopg.rows = rows
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setattr(node_registry, "_postgres_enabled", lambda: True)

    result = rotate_node_credentials("gpu-01")

    assert result["node_id"] == "gpu-01"
    assert result["credential_generation"] == 2
    # The outgoing certificate must be revoked, and the read must precede the UPDATE.
    crl = [entry for entry in statements if "node_revoked_certificates" in entry[0]]
    assert crl and crl[0][1][0] == "0AABBCC"
    select_index = next(i for i, entry in enumerate(statements)
                        if "SELECT certificate_serial FROM registered_nodes" in entry[0])
    update_index = next(i for i, entry in enumerate(statements)
                        if "UPDATE registered_nodes SET secret_hash=''" in entry[0])
    assert select_index < update_index


def test_lost_response_retry_over_real_http(monkeypatch, tmp_path):
    """End-to-end: Core commits the enrollment, the response never arrives, and the
    worker's own retry over a real HTTP socket still gets working credentials.

    This exercises the real `/api/nodes/register` handler over a socket instead of
    a stub, so it proves the retry contract rather than the retry wrapper alone.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.request import Request, urlopen

    from core.api.nodes import router as nodes_router
    from core.first_run import ensure_secret_store

    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    body = json.dumps({"registration_token": token["token"], "node_id": "gpu-01",
                       "enrollment_id": "enroll-http01", "capabilities": ["image_generation"],
                       "hardware": {"gpu": "RTX 4090"}, "version": "1.5.0",
                       "csr": csr}).encode("utf-8")
    delivered = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/api/nodes/register"
            delivered["count"] += 1
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if delivered["count"] == 1:
                # Commit the enrollment, then drop the connection without an answer.
                enroll_node(payload["registration_token"], payload["node_id"],
                            payload["capabilities"], payload["hardware"], payload["version"],
                            payload["csr"], payload.get("enrollment_id"))
                self.close_connection = True
                return
            result = enroll_node(payload["registration_token"], payload["node_id"],
                                 payload["capabilities"], payload["hardware"], payload["version"],
                                 payload["csr"], payload.get("enrollment_id"))
            body_bytes = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/nodes/register"
        with pytest.raises(Exception):
            urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10)
        with urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}),
                     timeout=10) as response:
            credentials = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()

    assert delivered["count"] == 2
    assert credentials["worker_id"] == "gpu-01"
    assert verify_node_token(credentials["worker_secret"], "gpu-01")
    assert verify_node_certificate("gpu-01", credentials["certificate_serial"])
    assert registered_nodes()[0]["runtime_status"] == "PENDING_SELF_TEST"
    assert ensure_secret_store()["internal_api_key"]
    assert nodes_router is not None


def test_lost_response_retry_requires_the_same_node_and_key(monkeypatch, tmp_path):
    """A burned token must not become a universal re-enrollment bypass."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr,
                enrollment_id="enroll-abc123")
    with pytest.raises(PermissionError, match="already used"):
        enroll_node(token["token"], "gpu-02", ["image_generation"], {}, "1.5.0",
                    create_node_csr("gpu-02", tmp_path / "pki2"), enrollment_id="enroll-abc123")
    with pytest.raises(PermissionError, match="already used"):
        enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr,
                    enrollment_id="enroll-zzz999")
    with pytest.raises(PermissionError, match="already used"):
        enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr)


def test_rotate_invalidates_credentials_until_the_node_renews(monkeypatch, tmp_path):
    """Issue #80: the advertised `rotate` action must actually work end to end."""
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enrolled = enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr)
    serial = registered_nodes()[0]["certificate_serial"]

    rotated = rotate_node_credentials("gpu-01")
    assert rotated["node_id"] == "gpu-01"
    assert rotated["runtime_status"] == "PENDING_SELF_TEST"
    assert not verify_node_token(enrolled["jwt"], "gpu-01")
    assert not verify_node_token(enrolled["worker_secret"], "gpu-01")
    assert not verify_node_certificate("gpu-01", serial)
    crl_text = subprocess.run(["openssl", "crl", "-in", str(tmp_path / "node-ca.crl"),
                               "-text", "-noout"], check=True,
                              capture_output=True, text=True).stdout.lower().replace(":", "")
    assert serial.lower().lstrip("0") in crl_text

    renewed = renew_node("gpu-01", csr)
    assert verify_node_token(renewed["worker_secret"], "gpu-01")
    with pytest.raises(KeyError):
        rotate_node_credentials("no-such-node")
    revoke_node("gpu-01")
    with pytest.raises(KeyError):
        rotate_node_credentials("gpu-01")


def test_rotate_action_endpoint_succeeds(monkeypatch, tmp_path):
    """The /api/nodes/{id}/actions `rotate` action must not fail with 422."""
    from fastapi import HTTPException

    from core.api.nodes import control_node
    from core.models import NodeAction
    from core.state import store

    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    enroll_node(token["token"], "gpu-01", ["image_generation"], {}, "1.5.0", csr)
    store.save_worker({"node_id": "gpu-01", "node_name": "gpu-01", "role": "gpu",
                       "status": "READY", "last_seen": "2026-09-29T00:00:00+00:00",
                       "capabilities": ["image_generation"]})
    try:
        result = control_node("gpu-01", NodeAction(action="rotate", reason="compromise"))
        assert result["node_id"] == "gpu-01"
        assert registered_nodes()[0]["runtime_status"] == "PENDING_SELF_TEST"
        # A rotate for an unknown node is a 404, never a silent success.
        with pytest.raises(HTTPException) as error:
            control_node("no-such-node", NodeAction(action="rotate"))
        assert error.value.status_code == 404
    finally:
        store.workers.pop("gpu-01", None)
