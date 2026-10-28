"""#355: disposable PKI credentials reach the actual Worker HTTP consumer."""
import json
import shutil
import subprocess
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import app
from core.node_registry import create_node_csr
from tests.acceptance_provenance import acceptance_provenance


def test_pki_enrollment_renew_expiry_revoke_at_worker_consumer(monkeypatch, tmp_path):
    git = shutil.which("git")
    if not shutil.which("openssl") and git:
        bundled = Path(git).parent.parent / "usr/bin/openssl.exe"
        if bundled.is_file():
            monkeypatch.setenv("PATH", str(bundled.parent) + os.pathsep + os.environ.get("PATH", ""))
    if not shutil.which("openssl"):
        if os.getenv("VERTEP_REQUIRE_PKI_CALLER_ACCEPTANCE") == "1":
            pytest.fail("OpenSSL is required for disposable credential consumer acceptance")
        pytest.skip("OpenSSL is unavailable; credential consumer proof is BLOCKED")
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "true")
    monkeypatch.setenv("NODE_CA_CERT_PATH", str(tmp_path / "pki/ca.crt"))
    monkeypatch.setenv("NODE_CA_KEY_PATH", str(tmp_path / "pki/ca.key"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    (tmp_path / "users.json").write_text("{}", encoding="utf-8")
    api = TestClient(app)
    security_before = api.get("/api/security/check")
    assert security_before.status_code == 200
    assert security_before.json()["checks"]["certificates"]["node_ca"]["status"] == "missing"
    token = api.post("/api/nodes/registration-tokens", json={"role": "gpu"}).json()["token"]
    csr = create_node_csr("trust-caller", tmp_path / "client")
    response = api.post("/api/nodes/register", json={"registration_token": token,
        "node_id": "trust-caller", "capabilities": ["image_generation"], "hardware": {},
        "version": "1.0.0", "csr": csr})
    assert response.status_code == 200, response.text
    issued = response.json()
    security_after = api.get("/api/security/check")
    assert security_after.status_code == 200
    assert security_after.json()["checks"]["certificates"]["node_ca"]["status"] == "ok"
    assert "node_ca is missing" not in security_after.json()["recommendation"]
    def headers(credentials, dn="CN=trust-caller"):
        return {"x-vertep-token": credentials["worker_secret"], "x-vertep-client-dn": dn,
                "x-vertep-client-serial": credentials["certificate_serial"]}
    def heartbeat(credentials, dn="CN=trust-caller"):
        return api.post("/api/workers/heartbeat", headers=headers(credentials, dn),
                        json={"node_name": "trust-caller", "role": "gpu", "status": "FREE"})
    assert heartbeat(issued).status_code == 200
    assert heartbeat(issued, "CN=another-node").status_code == 401
    registry = tmp_path / "node-registry.json"
    original = registry.read_text(encoding="utf-8")
    expired = json.loads(original)
    expired["nodes"]["trust-caller"]["certificate_expires_at"] = "Aug 20 12:00:00 2020 GMT"
    registry.write_text(json.dumps(expired), encoding="utf-8")
    assert heartbeat(issued).status_code == 401
    registry.write_text(original, encoding="utf-8")
    response = api.post("/api/nodes/trust-caller/renew", headers=headers(issued), json={"csr": csr})
    assert response.status_code == 200, response.text
    renewed = response.json()
    assert heartbeat(issued).status_code == 401
    assert heartbeat(renewed).status_code == 200
    assert api.post("/api/nodes/trust-caller/revoke").status_code == 200
    assert heartbeat(renewed).status_code == 401
    certificate = tmp_path / "renewed.crt"
    certificate.write_text(renewed["certificate"], encoding="utf-8")
    verified = subprocess.run(["openssl", "verify", "-crl_check", "-CAfile", str(tmp_path / "pki/ca.crt"),
        "-CRLfile", str(tmp_path / "node-ca.crl"), str(certificate)], capture_output=True, text=True)
    assert verified.returncode != 0
    assert "revoked" in (verified.stdout + verified.stderr).lower()
