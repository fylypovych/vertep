import json
import subprocess

import pytest

from core.node_registry import (create_registration_token, enroll_node, registered_nodes,
                                create_node_csr, renew_node, revoke_node, verify_node_token)


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
    assert not verify_node_token(enrolled["jwt"], "gpu-02")
    assert verify_node_token(enrolled["worker_secret"], "gpu-01")
    with pytest.raises(PermissionError, match="already used"):
        enroll_node(token["token"], "gpu-02", [], {}, "1.3.0", create_node_csr("gpu-02", tmp_path / "pki2"))
    assert registered_nodes()[0]["capabilities"] == ["image_generation"]
    assert "secret_hash" not in registered_nodes()[0]
    assert token["token"] not in (tmp_path / "node-registry.json").read_text()
    renewed = renew_node("gpu-01", csr)
    assert renewed["jwt"] != enrolled["jwt"]
    assert not verify_node_token(enrolled["jwt"], "gpu-01")
    assert verify_node_token(renewed["jwt"], "gpu-01")
    revoke_node("gpu-01")
    assert not verify_node_token(renewed["jwt"], "gpu-01")
    assert not verify_node_token(renewed["worker_secret"], "gpu-01")


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
