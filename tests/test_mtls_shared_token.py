"""i.0.0.4.84 (#484, remainder of #473): no shared NODE_API_TOKEN under mTLS.

With NODE_MTLS_REQUIRED=true a node must present its own credential; a valid
certificate identity plus the shared token is refused. Node-bound tokens/hashes,
revocation and the certificate check are kept; legacy non-mTLS mode keeps the
shared token for compatibility.
"""

import pytest

import core.security as security

SHARED = "shared-node-api-token"
OWN_A = "own-token-node-a"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("NODE_API_TOKEN", SHARED)
    for name in ("WORKER_TOKENS", "WORKER_TOKEN_HASHES", "REVOKED_TOKEN_HASHES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(security, "verify_node_token", lambda token, node: False)
    monkeypatch.setattr(security, "verify_node_certificate", lambda node, serial: serial == f"serial-{node}")


def _check(node, token, *, cn=None, serial=None):
    return security._valid_worker_token(node, token, f"CN={cn or node}", serial or f"serial-{node}")


@pytest.fixture
def mtls(monkeypatch):
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "true")


def test_valid_certificate_without_own_token_rejects_the_shared_token(mtls):
    """The audit reproduction: certificate OK, no individual token, shared token → False."""
    assert _check("node-a", SHARED) is False


@pytest.mark.parametrize("node", ["node-a", "node-b", "voice-01"])
def test_shared_token_is_refused_for_every_node_name(mtls, node):
    assert _check(node, SHARED) is False


def test_own_worker_token_still_works_under_mtls(mtls, monkeypatch):
    monkeypatch.setenv("WORKER_TOKENS", f"node-a:{OWN_A}")
    assert _check("node-a", OWN_A) is True
    assert _check("node-a", SHARED) is False
    assert _check("node-b", OWN_A) is False


def test_own_token_hash_still_works_under_mtls(mtls, monkeypatch):
    monkeypatch.setenv("WORKER_TOKEN_HASHES", f"node-a:{security._hash_secret(OWN_A)}")
    assert _check("node-a", OWN_A) is True
    assert _check("node-a", SHARED) is False


def test_issued_node_credential_still_works_under_mtls(mtls, monkeypatch):
    monkeypatch.setattr(security, "verify_node_token", lambda token, node: token == "issued" and node == "node-a")
    assert _check("node-a", "issued") is True


def test_revoked_token_is_refused(mtls, monkeypatch):
    monkeypatch.setenv("WORKER_TOKENS", f"node-a:{OWN_A}")
    monkeypatch.setenv("REVOKED_TOKEN_HASHES", security._hash_secret(OWN_A))
    assert _check("node-a", OWN_A) is False


@pytest.mark.parametrize("cn,serial", [("node-b", None), ("node-a", "serial-other")])
def test_mismatching_certificate_is_refused_even_with_own_token(mtls, monkeypatch, cn, serial):
    monkeypatch.setenv("WORKER_TOKENS", f"node-a:{OWN_A}")
    assert _check("node-a", OWN_A, cn=cn, serial=serial) is False


def test_missing_token_is_refused(mtls):
    assert _check("node-a", "") is False


def test_legacy_non_mtls_mode_keeps_the_shared_token(monkeypatch):
    monkeypatch.delenv("NODE_MTLS_REQUIRED", raising=False)
    assert security._valid_worker_token("node-a", SHARED) is True
    assert security._valid_worker_token("node-a", "wrong") is False
    monkeypatch.setenv("WORKER_TOKENS", f"node-a:{OWN_A}")
    # A node with its own token uses it; the shared token no longer applies to it.
    assert security._valid_worker_token("node-a", SHARED) is False
    assert security._valid_worker_token("node-a", OWN_A) is True
