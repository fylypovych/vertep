"""The resource contract over a real HTTP endpoint (Issue #250, L3/L6).

The lease boundary (`status → request → grant → heartbeat → revoke → release`)
is exercised through real sockets: `VertepResourceClient` and `LeaseManager`
talk to a controlled in-process production endpoint that authenticates the lab
token, enforces the contract version and applies production priority.  No stub
openers and no fixture dicts stand in for the transport.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lab.gpu_lease import (CONTRACT_VERSION, TOKEN_HEADER, LeaseDenied, LeaseLost,
                           LeaseManager, ResourceRequirement, ResourceUnavailable,
                           VertepResourceClient)

REQUIREMENT = ResourceRequirement(model="lab-llm", vram_mb=1024)


class _ProductionState:
    """Server-side production state: priority, leases, authentication."""

    def __init__(self, *, token="lab-token", queue_depth=0, reserved=False,
                 gpu_busy=False, free_vram_mb=24576, answer_contract=CONTRACT_VERSION,
                 revoke_after_heartbeats=0, expire_on_grant=False):
        self.token = token
        self.queue_depth = queue_depth
        self.reserved = reserved
        self.gpu_busy = gpu_busy
        self.free_vram_mb = free_vram_mb
        self.answer_contract = answer_contract
        self.revoke_after_heartbeats = revoke_after_heartbeats
        self.expire_on_grant = expire_on_grant
        self.leases = {}
        self.revoked = set()
        self.released = []
        self.seen = []            # [(method, path, contract_header, token_header)]
        self.heartbeats = 0
        self._lock = threading.Lock()
        self._counter = 0

    def deny_reason(self):
        if self.queue_depth > 0:
            return "production queue is not empty"
        if self.reserved:
            return "GPU is reserved for production"
        if self.gpu_busy:
            return "GPU is busy with production work"
        return ""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the pytest output clean
        pass

    @property
    def state(self):
        return self.server.state

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8") or "{}") if raw else {}

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self):
        state = self.state
        contract = self.headers.get("X-Vertep-Contract-Version")
        token = self.headers.get(TOKEN_HEADER)
        state.seen.append((self.command, self.path, contract, token))
        if contract != CONTRACT_VERSION:
            self._send(400, {"error": "unsupported contract version"})
            return False
        if state.token and token != state.token:
            self._send(403, {"error": "invalid lab token"})
            return False
        return True

    def _base(self, state):
        return {"contract_version": state.answer_contract, "provider": "vertep",
                "free_vram_mb": state.free_vram_mb, "queue_depth": state.queue_depth,
                "reserved": state.reserved, "gpu_busy": state.gpu_busy}

    def do_GET(self):
        if self.path != "/status" or not self._authenticated():
            return
        state = self.state
        payload = self._base(state)
        payload.update({
            "gpu_available": state.deny_reason() == "" and state.free_vram_mb > 0,
            "active_leases": sorted(state.leases),
            "reason": state.deny_reason(),
            "idle_sharing_enabled": True,
        })
        self._send(200, payload)

    def do_POST(self):
        if not self._authenticated():
            return
        state = self.state
        body = self._read_json()
        with state._lock:
            if self.path == "/lease/request":
                self._request(state, body)
            elif self.path == "/lease/heartbeat":
                self._heartbeat(state, body)
            elif self.path == "/lease/release":
                self._release(state, body)
            else:
                self._send(404, {"error": "unknown path"})

    def _request(self, state, body):
        requirement = body.get("requirement") or {}
        ttl = int(body.get("ttl_seconds") or 60)
        reason = state.deny_reason()
        needed = int(requirement.get("vram_mb") or 0)
        if not reason and needed > state.free_vram_mb:
            reason = (f"model needs {needed} MB, only {state.free_vram_mb} MB free")
        if reason:
            self._send(200, {"contract_version": state.answer_contract,
                             "granted": False, "lease": None, "reason": reason})
            return
        state._counter += 1
        now = datetime.now(timezone.utc)
        expires = now - timedelta(seconds=1) if state.expire_on_grant else now + timedelta(seconds=ttl)
        lease = {"lease_id": f"lease-{state._counter:04d}",
                 "endpoint": "http://gpu.local:11434",
                 "model": str(requirement.get("model") or ""),
                 "granted_at": now.isoformat(), "expires_at": expires.isoformat(),
                 "heartbeat_seconds": 30, "state": "GRANTED", "vram_mb": needed}
        state.leases[lease["lease_id"]] = lease
        self._send(200, {"contract_version": state.answer_contract,
                         "granted": True, "lease": lease, "reason": ""})

    def _heartbeat(self, state, body):
        lease_id = str(body.get("lease_id") or "")
        ttl = int(body.get("ttl_seconds") or 60)
        lease = state.leases.get(lease_id)
        if lease is None:
            self._send(200, {"contract_version": state.answer_contract,
                             "active": False, "revoked": False, "expires_at": "",
                             "reason": "unknown lease"})
            return
        if lease_id in state.revoked:
            lease["state"] = "RELEASED"
            self._send(200, {"contract_version": state.answer_contract,
                             "active": False, "revoked": True,
                             "expires_at": lease["expires_at"],
                             "reason": "production work arrived: lease revoked"})
            return
        if datetime.now(timezone.utc) >= datetime.fromisoformat(lease["expires_at"]):
            self._send(200, {"contract_version": state.answer_contract,
                             "active": False, "revoked": False,
                             "expires_at": lease["expires_at"],
                             "reason": "lease expired"})
            return
        state.heartbeats += 1
        if (state.revoke_after_heartbeats
                and state.heartbeats >= state.revoke_after_heartbeats):
            state.revoked.add(lease_id)
            self._send(200, {"contract_version": state.answer_contract,
                             "active": False, "revoked": True,
                             "expires_at": lease["expires_at"],
                             "reason": "production work arrived: lease revoked"})
            return
        lease["expires_at"] = (datetime.now(timezone.utc)
                               + timedelta(seconds=ttl)).isoformat()
        self._send(200, {"contract_version": state.answer_contract,
                         "active": True, "revoked": False,
                         "expires_at": lease["expires_at"], "reason": ""})

    def _release(self, state, body):
        lease_id = str(body.get("lease_id") or "")
        lease = state.leases.pop(lease_id, None)
        state.released.append(lease_id)
        if lease is None:
            self._send(200, {"contract_version": state.answer_contract,
                             "released": False, "reason": "unknown lease"})
            return
        state.free_vram_mb += int(lease.get("vram_mb") or 0)
        self._send(200, {"contract_version": state.answer_contract,
                         "released": True, "reason": "",
                         "free_vram_mb": state.free_vram_mb})


@pytest.fixture
def production(tmp_path):
    state = _ProductionState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % server.server_address[1]

    class Endpoint:
        def __init__(self):
            self.url = url
            self.state = state

        def client(self, *, token="lab-token", enabled=True):
            return VertepResourceClient(url, token=token, enabled=enabled, timeout=5)

        def stop(self):
            if getattr(self, "_stopped", False):
                return
            self._stopped = True
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    endpoint = Endpoint()
    try:
        yield endpoint
    finally:
        endpoint.stop()


# -- the contract really runs over sockets -------------------------------
def test_status_reaches_production_with_token_and_contract_headers(production):
    answer = production.client().status()
    assert answer["gpu_available"] is True
    assert answer["contract_version"] == CONTRACT_VERSION
    method, path, contract, token = production.state.seen[0]
    assert (method, path) == ("GET", "/status")
    assert contract == CONTRACT_VERSION
    assert token == "lab-token"


def test_full_lease_lifecycle_over_the_real_endpoint(production, tmp_path):
    manager = LeaseManager(production.client(), state_dir=tmp_path,
                           ttl_seconds=60)
    lease = manager.acquire(REQUIREMENT)
    assert lease.lease_id.startswith("lease-")
    assert manager.heartbeat() is True
    manager.assert_usable()
    outcome = manager.release(reason="inference finished")
    assert outcome["released"] is True
    paths = [path for _, path, _, _ in production.state.seen]
    assert paths == ["/lease/request", "/lease/heartbeat", "/lease/release"]
    assert production.state.leases == {}
    assert lease.lease_id in production.state.released


def test_wrong_or_missing_token_is_refused_by_production(production):
    for token in ("", "not-the-token"):
        with pytest.raises(ResourceUnavailable) as error:
            production.client(token=token).status()
        assert "HTTP" in str(error.value)
    assert production.state.seen and production.state.seen[-1][3] == "not-the-token"
    assert production.state.leases == {}


def test_unsupported_answer_contract_version_is_refused(production):
    production.state.answer_contract = "99"
    with pytest.raises(ResourceUnavailable) as error:
        production.client().status()
    assert "contract version" in str(error.value)


def test_disabled_client_never_reaches_the_endpoint(production):
    with pytest.raises(ResourceUnavailable):
        production.client(enabled=False).status()
    assert production.state.seen == []


# -- production priority over the real endpoint --------------------------
@pytest.mark.parametrize(
    "field,value,reason",
    [("queue_depth", 1, "production queue is not empty"),
     ("reserved", True, "GPU is reserved for production"),
     ("gpu_busy", True, "GPU is busy with production work")],
)
def test_lease_is_denied_under_production_pressure(production, field, value, reason):
    setattr(production.state, field, value)
    manager = LeaseManager(production.client(), state_dir=None)
    with pytest.raises(LeaseDenied) as error:
        manager.acquire(REQUIREMENT)
    assert reason in str(error.value)
    assert production.state.leases == {}


def test_revoke_after_production_work_stops_the_lab(production):
    production.state.revoke_after_heartbeats = 1
    manager = LeaseManager(production.client(), state_dir=None)
    manager.acquire(REQUIREMENT)
    with pytest.raises(LeaseLost) as error:
        manager.heartbeat()
    assert "revoked" in str(error.value)
    assert manager.lease is None
    assert manager.stopped_at_control_point is True
    # the lab confirmed the release back to production
    assert production.state.released


def test_expired_lease_forbids_inference_over_the_real_endpoint(production):
    production.state.expire_on_grant = True
    manager = LeaseManager(production.client(), state_dir=None)
    lease = manager.acquire(REQUIREMENT)
    with pytest.raises(LeaseLost):
        manager.assert_usable()
    assert manager.lease is None
    assert lease.lease_id in production.state.released


def test_connection_loss_is_reported_as_lease_lost(production):
    manager = LeaseManager(production.client(), state_dir=None)
    manager.acquire(REQUIREMENT)
    production.stop()
    with pytest.raises(LeaseLost) as error:
        manager.heartbeat()
    assert "unreachable" in str(error.value)
    assert manager.lease is None


# -- crash recovery over the real endpoint --------------------------------
def test_restart_reconciliation_releases_the_persisted_lease(production, tmp_path):
    first = LeaseManager(production.client(), state_dir=tmp_path)
    lease = first.acquire(REQUIREMENT)
    assert lease.lease_id in production.state.leases

    restarted = LeaseManager(production.client(), state_dir=tmp_path)
    assert restarted.lease is None
    assert restarted.stopped_at_control_point is True
    assert lease.lease_id in production.state.released
    assert lease.lease_id not in production.state.leases
