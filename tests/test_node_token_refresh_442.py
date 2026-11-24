"""#442 D12: a node JWT lives 24 h; the Worker renews it with its secret instead of being
refused (401) by every call from the next day on (stand 22.11.2026, node OFFLINE ever since)."""
import json
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from core.node_registry import create_node_csr, create_registration_token, enroll_node
from core.security import _valid_worker_token
from worker.enrollment import _jwt_expiry, refresh_node_token

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="node PKI needs openssl")


@pytest.fixture
def enrolled(monkeypatch, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "installation.json").write_text('{"completed_at": "2026-11-24T00:00:00Z"}')
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "false")
    monkeypatch.setenv("NODE_TOKEN_TTL_SECONDS", "2")
    token = create_registration_token("text", 900)["token"]
    credentials = enroll_node(token, "cpu-node", ["text_generation"], {}, "0.0.4.42",
                              create_node_csr("cpu-node", tmp_path / "pki"))
    path = tmp_path / "node-credentials.json"
    path.write_text(json.dumps(credentials))
    return credentials, path


def test_an_expired_jwt_is_refused_and_the_secret_gets_a_new_one(enrolled, monkeypatch):
    from core.app import app
    credentials, path = enrolled
    time.sleep(3)
    assert not _valid_worker_token("cpu-node", credentials["jwt"])  # the day-old JWT: 401
    core = TestClient(app)
    assert core.post("/api/nodes/cpu-node/token", headers={"X-Vertep-Token": credentials["jwt"]}).status_code == 401
    assert core.post("/api/nodes/other-node/token",
                     headers={"X-Vertep-Token": credentials["worker_secret"]}).status_code == 401

    monkeypatch.setenv("NODE_TOKEN_TTL_SECONDS", "86400")
    client = core  # TestClient is an httpx.Client
    fresh = refresh_node_token(client, "", "cpu-node", credentials["jwt"], path)
    assert fresh != credentials["jwt"] and _jwt_expiry(fresh) > time.time() + 3600
    assert _valid_worker_token("cpu-node", fresh)
    assert json.loads(path.read_text())["jwt"] == fresh  # kept for the next start
    assert refresh_node_token(client, "", "cpu-node", fresh, path) == fresh  # still valid: no call
