"""The Worker on the CORE host enrolls with mTLS like any node (owner decision 20.11.2026).

Regression: with NODE_MTLS_REQUIRED the local Worker called http://core:8080 with the
shared token and every heartbeat was refused (401), so a single-server installation had
no node: modules could not be placed and jobs never left the queue.
"""

import json
import os
import shutil
import stat

import pytest

from core.local_node import ENROLLMENT_FILE, ensure_local_worker_enrollment
from core.node_registry import create_node_csr, create_registration_token, enroll_node, registered_nodes
from worker.enrollment import local_core_connection

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="node PKI needs openssl")

PLAN = {"schema": 1, "role": "core", "services": ["core", "proxy", "worker"],
        "additional_roles": ["backup", "monitoring", "text"]}


@pytest.fixture
def core_host(monkeypatch, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "deployment-plan.json").write_text(json.dumps(PLAN))
    certificate = tmp_path / "vertep.crt"
    certificate.write_text("-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n")
    for name, value in {"CONFIG_ROOT": str(config), "NODE_MTLS_REQUIRED": "true", "NODE_NAME": "vertep",
                        "WEB_DOMAIN": "10.10.10.10", "CORE_CERTIFICATE_PATH": str(certificate)}.items():
        monkeypatch.setenv(name, value)
    for name in ("CORE_URL", "REGISTRATION_TOKEN", "CORE_CA_PATH"):
        # setenv first so monkeypatch restores the original state after the Worker code set it.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return config


def test_core_offers_its_worker_a_single_use_enrollment_and_the_worker_enrolls_as_core(core_host, tmp_path):
    result = ensure_local_worker_enrollment()
    assert result["state"] == "offered" and result.get("created")
    offer_path = core_host / ENROLLMENT_FILE
    assert stat.S_IMODE(offer_path.stat().st_mode) == 0o600
    offer = json.loads(offer_path.read_text())
    assert offer["node_id"] == "vertep" and offer["core_url"] == "https://10.10.10.10:8443"
    # A valid offer is not replaced on the next watchdog pass.
    assert ensure_local_worker_enrollment().get("created") is None

    # The Worker side: the local default URL becomes the proxy, CORE is pinned, the token is used.
    url = local_core_connection("http://core:8080", core_host / "node-credentials.json")
    assert url == "https://10.10.10.10:8443"
    assert os.environ["REGISTRATION_TOKEN"] == offer["token"]
    assert os.environ["CORE_CA_PATH"].endswith("pki/core-server.crt")

    # Enrollment with that token gives the node role core with the roles of its plan.
    enrolled = enroll_node(offer["token"], "vertep", ["text_generation", "backup"], {}, "0.0.4.23",
                           create_node_csr("vertep", tmp_path / "worker-pki"))
    assert enrolled["role"] == "core" and enrolled["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert "text_generation" in enrolled["configuration"]["capabilities"]
    assert [node["node_id"] for node in registered_nodes()] == ["vertep"]
    assert ensure_local_worker_enrollment()["state"] == "enrolled"


def test_the_core_role_is_never_offered_through_the_registration_api(core_host):
    with pytest.raises(ValueError):
        create_registration_token("core", 900)


def test_nothing_changes_without_mtls_or_for_a_configured_remote_node(core_host, monkeypatch):
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "false")
    assert ensure_local_worker_enrollment()["state"] == "not_applicable"
    assert not (core_host / ENROLLMENT_FILE).exists()
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "true")
    ensure_local_worker_enrollment()
    # A remote node has its own CORE_URL: the local offer never redirects it.
    monkeypatch.setenv("CORE_URL", "https://core.example:8443")
    assert local_core_connection("https://core.example:8443", core_host / "node-credentials.json") == "https://core.example:8443"
    assert "REGISTRATION_TOKEN" not in os.environ


def test_an_offer_for_another_node_is_ignored(core_host, monkeypatch):
    ensure_local_worker_enrollment()
    monkeypatch.setenv("NODE_NAME", "other-node")
    assert local_core_connection("http://core:8080", core_host / "node-credentials.json") == "http://core:8080"
