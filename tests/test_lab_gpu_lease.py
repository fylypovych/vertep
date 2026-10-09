"""GPU idle-lease contract proofs (Issue #107, stages 3–4).

The laboratory must only touch a GPU under a lease granted by the production
dispatcher, and Vertep has absolute priority.  These tests cover the whole
contract with the deterministic production stand-in: grant, deny under queue
pressure/reservation/VRAM shortage, lease expiry, revoke mid-inference,
connection loss, release confirmation and restart reconciliation — plus the
fail-closed behaviour of the real HTTP adapter while idle sharing is off.
"""

from datetime import datetime, timedelta, timezone

import pytest

from lab.gpu_lease import (CONTRACT_VERSION, FakeResourceProvider, LeaseDenied, LeaseLost,
                           LeaseManager, ResourceRequirement, ResourceUnavailable,
                           VertepResourceClient)

REQUIREMENT = ResourceRequirement(model="qwen2.5-coder:7b", vram_mb=8192)


@pytest.fixture()
def manager(tmp_path):
    return LeaseManager(FakeResourceProvider(), state_dir=tmp_path)


def test_status_reports_the_contract_version(manager):
    status = manager.status()
    assert status["contract_version"] == CONTRACT_VERSION
    assert status["gpu_available"] is True


def test_grant_then_release_confirms_the_resource_is_free(manager):
    lease = manager.acquire(REQUIREMENT)
    assert lease.state.value == "GRANTED"
    assert manager.assert_usable().lease_id == lease.lease_id
    assert manager.release()["released"] is True
    assert manager.lease is None


def test_lease_is_denied_when_the_production_queue_is_not_empty(tmp_path):
    manager = LeaseManager(FakeResourceProvider(queue_depth=2), state_dir=tmp_path)
    with pytest.raises(LeaseDenied) as error:
        manager.acquire(REQUIREMENT)
    assert "queue" in str(error.value)
    assert manager.lease is None


def test_lease_is_denied_when_the_gpu_is_reserved_for_production(tmp_path):
    manager = LeaseManager(FakeResourceProvider(reserved=True), state_dir=tmp_path)
    with pytest.raises(LeaseDenied):
        manager.acquire(REQUIREMENT)


def test_lease_is_denied_when_the_gpu_is_busy_with_production_work(tmp_path):
    manager = LeaseManager(FakeResourceProvider(gpu_busy=True), state_dir=tmp_path)
    with pytest.raises(LeaseDenied):
        manager.acquire(REQUIREMENT)


def test_lease_is_denied_when_the_model_does_not_fit(tmp_path):
    manager = LeaseManager(FakeResourceProvider(vram_mb=4096), state_dir=tmp_path)
    with pytest.raises(LeaseDenied) as error:
        manager.acquire(REQUIREMENT)
    assert "MB" in str(error.value)


def test_heartbeat_renews_the_lease(manager):
    lease = manager.acquire(REQUIREMENT)
    original = lease.expires_at
    assert manager.heartbeat() is True
    assert manager.lease.expires_at >= original


def test_revoke_makes_the_laboratory_stop_immediately(tmp_path):
    provider = FakeResourceProvider(revoke_after_heartbeats=1)
    manager = LeaseManager(provider, state_dir=tmp_path)
    lease = manager.acquire(REQUIREMENT)
    with pytest.raises(LeaseLost):
        manager.heartbeat()
    assert manager.lease is None
    assert manager.stopped_at_control_point is True
    assert lease.lease_id in provider.released


def test_production_work_denies_new_requests_and_revokes_held_leases(tmp_path):
    provider = FakeResourceProvider()
    manager = LeaseManager(provider, state_dir=tmp_path)
    manager.acquire(REQUIREMENT)
    provider.simulate_production_work()

    with pytest.raises(LeaseLost):
        manager.heartbeat()
    other = LeaseManager(provider, state_dir=tmp_path / "other")
    with pytest.raises(LeaseDenied):
        other.acquire(REQUIREMENT)


def test_local_lease_expiry_forbids_further_inference(tmp_path):
    provider = FakeResourceProvider()
    manager = LeaseManager(provider, state_dir=tmp_path)
    lease = manager.acquire(REQUIREMENT)
    # Simulate TTL decay: the server-side record expires, but the local copy is
    # the authoritative guard for new inferences.
    manager.lease.expires_at = (datetime.now(timezone.utc)
                                - timedelta(seconds=1)).isoformat()
    with pytest.raises(LeaseLost):
        manager.assert_usable()
    assert manager.lease is None


def test_inference_without_a_lease_is_forbidden(tmp_path):
    manager = LeaseManager(FakeResourceProvider(), state_dir=tmp_path)
    with pytest.raises(LeaseLost):
        manager.assert_usable()


def test_connection_loss_drops_the_lease_fail_closed(tmp_path):
    provider = FakeResourceProvider()
    manager = LeaseManager(provider, state_dir=tmp_path)
    manager.acquire(REQUIREMENT)
    provider.offline = True
    with pytest.raises(LeaseLost):
        manager.heartbeat()
    assert manager.lease is None
    with pytest.raises(LeaseDenied):
        LeaseManager(provider, state_dir=tmp_path / "second").acquire(REQUIREMENT)


def test_status_survives_an_unreachable_production_endpoint(tmp_path):
    manager = LeaseManager(FakeResourceProvider(offline=True), state_dir=tmp_path)
    status = manager.status()
    assert status["gpu_available"] is False
    assert status["reason"]


def test_restart_reconciles_a_lease_instead_of_keeping_the_gpu(tmp_path):
    provider = FakeResourceProvider()
    first = LeaseManager(provider, state_dir=tmp_path)
    lease = first.acquire(REQUIREMENT)
    restarted = LeaseManager(provider, state_dir=tmp_path)
    assert restarted.lease is None
    assert lease.lease_id in provider.released
    assert restarted.stopped_at_control_point is True


def test_context_manager_always_releases(manager):
    with pytest.raises(RuntimeError):
        with manager:
            manager.acquire(REQUIREMENT)
            raise RuntimeError("inference failed")
    assert manager.lease is None


# -- the real adapter is fail-closed --------------------------------------
@pytest.mark.parametrize("call_name", ["status", "request", "heartbeat", "release"])
def test_http_adapter_refuses_every_call_while_idle_sharing_is_disabled(call_name):
    client = VertepResourceClient("https://gpu.example", enabled=False)
    calls = {"status": client.status,
             "request": lambda: client.request(REQUIREMENT, 60),
             "heartbeat": lambda: client.heartbeat("lease-1", 60),
             "release": lambda: client.release("lease-1")}
    with pytest.raises(ResourceUnavailable):
        calls[call_name]()


def test_http_adapter_requires_a_configured_endpoint():
    client = VertepResourceClient("", enabled=True)
    with pytest.raises(ResourceUnavailable):
        client.status()


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self):
        import json
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_http_adapter_speaks_the_versioned_contract():
    captured = {}

    class Opener:
        def open(self, request, timeout=None):
            captured["path"] = request.full_url
            captured["version"] = request.headers.get("X-vertep-contract-version")
            captured["token"] = request.headers.get("X-vertep-lab-token")
            return _FakeResponse({"contract_version": CONTRACT_VERSION,
                                  "gpu_available": True})

    client = VertepResourceClient("https://gpu.example", token="lab-token", enabled=True,
                                  opener=Opener())
    assert client.status()["gpu_available"] is True
    assert captured["path"].endswith("/status")
    assert captured["version"] == CONTRACT_VERSION
    assert captured["token"] == "lab-token"


def test_http_adapter_rejects_a_different_contract_version():
    class Opener:
        def open(self, request, timeout=None):
            return _FakeResponse({"contract_version": "99"})

    client = VertepResourceClient("https://gpu.example", enabled=True, opener=Opener())
    with pytest.raises(ResourceUnavailable):
        client.status()

