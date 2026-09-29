import json
import shutil
import subprocess

import pytest

from core.node_registry import (create_registration_token, enroll_node, registered_nodes,
                                create_node_csr, record_self_test, renew_node, revoke_node,
                                verify_node_certificate, verify_node_token)


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="node PKI integration tests require openssl"
)


def test_registration_token_is_one_time_and_issues_bound_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    token = create_registration_token("gpu", 900)
    csr = create_node_csr("gpu-01", tmp_path / "client-pki")
    assert token["token"].startswith("VT-")
    enrolled = enroll_node(token["token"], "gpu-01", ["image_generation", "video_generation"],
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
