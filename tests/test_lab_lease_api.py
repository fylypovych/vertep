"""Production side of the laboratory GPU lease and the one-command lab install."""

import asyncio
import subprocess
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.api import lab_lease

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"x-vertep-lab-token": "secret"}
GPU = {"node_name": "gpu", "capabilities": ["image_generation"], "vram_mb": 8192, "free_vram_mb": 8192}


class Queue:
    ready = 0
    running = 0

    def depth(self):
        return self.ready

    def inflight_depth(self):
        return self.running


class Store:
    workers = {"gpu": dict(GPU)}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LAB_IDLE_SHARING_ENABLED", "true")
    monkeypatch.setenv("LAB_LEASE_TOKEN", "secret")
    monkeypatch.setenv("LAB_GPU_ENDPOINT", "http://gpu.local:11434")
    queue = Queue()
    monkeypatch.setattr(lab_lease, "task_queue", queue)
    monkeypatch.setattr(lab_lease, "store", Store())
    monkeypatch.setattr(lab_lease, "dispatch_allowed", lambda: True)
    lab_lease._leases.clear()
    app = FastAPI()
    app.include_router(lab_lease.router)
    test_client = TestClient(app)
    test_client.queue = queue
    yield test_client
    lab_lease._leases.clear()


def _request(client, vram=4096):
    return client.post("/lab-lease/v1/lease/request", headers=HEADERS,
                       json={"requirement": {"model": "m", "vram_mb": vram}, "ttl_seconds": 60}).json()


def test_token_is_required(client):
    assert client.get("/lab-lease/v1/status").status_code == 401


def test_disabled_by_default_denies(client, monkeypatch):
    monkeypatch.delenv("LAB_IDLE_SHARING_ENABLED")
    assert _request(client)["granted"] is False


def test_grant_only_when_production_is_idle(client):
    client.queue.ready = 1
    assert _request(client)["reason"] == "production queue is not empty"
    client.queue.ready, client.queue.running = 0, 1
    assert _request(client)["reason"] == "GPU is busy with production work"
    client.queue.running = 0
    assert _request(client, vram=99999)["granted"] is False
    assert _request(client)["granted"] is True
    assert _request(client)["reason"] == "another laboratory lease is active"


def test_production_work_revokes_holds_and_release_frees_the_gpu(client):
    lease_id = _request(client)["lease"]["lease_id"]
    beat = lambda: client.post("/lab-lease/v1/lease/heartbeat", headers=HEADERS,
                               json={"lease_id": lease_id, "ttl_seconds": 60}).json()
    assert beat()["active"] is True
    assert lab_lease.hold_gpu_for_laboratory(GPU) is False
    client.queue.ready = 1
    assert lab_lease.hold_gpu_for_laboratory(GPU) is True
    assert lab_lease.hold_gpu_for_laboratory({"capabilities": ["publishing"]}) is False
    assert beat()["revoked"] is True
    released = client.post("/lab-lease/v1/lease/release", headers=HEADERS, json={"lease_id": lease_id}).json()
    assert released["released"] is True
    assert lab_lease.hold_gpu_for_laboratory(GPU) is False


def test_laboratory_that_never_releases_loses_the_gpu_after_grace(client, monkeypatch):
    monkeypatch.setenv("LAB_LEASE_RELEASE_GRACE_SECONDS", "0")
    _request(client)
    client.queue.ready = 1
    lab_lease.hold_gpu_for_laboratory(GPU)
    assert lab_lease.hold_gpu_for_laboratory(GPU) is False
    assert lab_lease._leases == {}


def test_lab_client_speaks_the_same_contract(client):
    from lab.gpu_lease import LeaseManager, ResourceRequirement, ResourceProvider

    class Provider(ResourceProvider):
        def request(self, requirement, ttl_seconds):
            return client.post("/lab-lease/v1/lease/request", headers=HEADERS,
                               json={"requirement": requirement.as_dict(), "ttl_seconds": ttl_seconds}).json()

        def heartbeat(self, lease_id, ttl_seconds):
            return client.post("/lab-lease/v1/lease/heartbeat", headers=HEADERS,
                               json={"lease_id": lease_id, "ttl_seconds": ttl_seconds}).json()

        def release(self, lease_id):
            return client.post("/lab-lease/v1/lease/release", headers=HEADERS, json={"lease_id": lease_id}).json()

    manager = LeaseManager(Provider())
    assert manager.acquire(ResourceRequirement(model="m", vram_mb=1024)).endpoint == "/lab-lease/v1/inference"
    assert manager.heartbeat() is True
    assert manager.release()["released"] is True


def _inference(client, lease_id):
    return client.post("/lab-lease/v1/inference/api/generate",
                       headers={**HEADERS, "x-vertep-lab-lease": lease_id},
                       json={"model": "m", "prompt": "hi"})


class _Gpu:
    """Stand-in for the GPU endpoint; records forwarded calls."""
    calls: list = []
    release = None

    def __init__(self, **_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, json):
        _Gpu.calls.append((url, json))
        if _Gpu.release is not None:
            await _Gpu.release.wait()
        return httpx.Response(200, json={"response": "ok"}, request=httpx.Request("POST", url))


@pytest.fixture
def gpu(monkeypatch):
    _Gpu.calls, _Gpu.release = [], None
    monkeypatch.setattr(lab_lease.httpx, "AsyncClient", _Gpu)
    return _Gpu


def test_inference_needs_a_granted_lease_and_forwards_without_streaming(client, gpu):
    assert _inference(client, "none").status_code == 409
    assert gpu.calls == []
    lease_id = _request(client)["lease"]["lease_id"]
    assert _inference(client, lease_id).json() == {"response": "ok"}
    assert gpu.calls[0][0] == "http://gpu.local:11434/api/generate"
    assert gpu.calls[0][1]["stream"] is False
    assert client.post("/lab-lease/v1/inference/api/pull", headers=HEADERS, json={}).status_code == 404


def test_revoked_lease_cancels_the_running_inference(client, gpu):
    lease_id = _request(client)["lease"]["lease_id"]
    gpu.release = asyncio.Event()
    client.queue.ready = 1
    response = _inference(client, lease_id)
    assert response.status_code == 409
    assert lab_lease._leases[lease_id]["state"] == "REVOKING"


def test_release_unloads_the_model_and_grant_frees_comfyui(client, gpu, monkeypatch):
    monkeypatch.setenv("LAB_GPU_FREE_URL", "http://comfyui:8188/free")
    lease_id = _request(client)["lease"]["lease_id"]
    assert gpu.calls[0][0] == "http://comfyui:8188/free"
    client.post("/lab-lease/v1/lease/release", headers=HEADERS, json={"lease_id": lease_id})
    assert gpu.calls[-1] == ("http://gpu.local:11434/api/generate", {"model": "m", "keep_alive": 0})


def test_role_service_runs_report_tasks_only_and_needs_a_lease(monkeypatch, tmp_path):
    from lab import role_service

    monkeypatch.setenv("LAB_ROLE_EXECUTOR_TOKEN", "t")
    monkeypatch.setattr(role_service, "_call_model",
                        lambda model, prompt, lease, limits: {"response": "звіт", "eval_count": 5})
    service = TestClient(role_service.create_app())
    headers = {"x-lab-executor-token": "t"}
    assert service.post("/invoke", json={}).status_code == 401
    assert service.post("/invoke", headers=headers, json={"task": {"kind": "fix"}}).json()["status"] == "blocked"
    assert service.post("/invoke", headers=headers, json={"task": {"kind": "audit"}}).json()["status"] == "blocked"
    answer = service.post("/invoke", headers=headers, json={
        "task": {"kind": "audit"}, "model": "ollama:m", "role": "reviewer",
        "resource_lease": {"lease_id": "l"}, "workspace": {"root": str(tmp_path)}}).json()
    assert answer == {"status": "ok", "tokens_used": 5, "result": {"summary": "звіт"}}


def test_role_service_never_reads_outside_the_workspace(tmp_path):
    from lab.role_service import _workspace_context

    (tmp_path / "secret.txt").write_text("SECRET")
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("code")
    context = _workspace_context(str(work), ["a.py", "../secret.txt"])
    assert "code" in context and "SECRET" not in context


def test_real_app_routes_lease_without_user_session():
    from core.app import app

    assert any(getattr(route, "path", "") == "/lab-lease/v1/status" for route in app.routes)
    response = TestClient(app).get("/lab-lease/v1/status", headers={"x-vertep-lab-token": "wrong"})
    assert response.status_code == 401
    assert response.text.find("Invalid laboratory token") >= 0


def test_install_script_is_valid_and_keeps_isolation():
    script = (ROOT / "bootstrap-lab.sh").read_text(encoding="utf-8")
    assert "docker.sock" not in script
    assert "use a separate VM" in script
    assert "LAB_IDLE_SHARING_ENABLED=false" in script
    bash = shutil.which("bash")
    if bash:
        subprocess.run([bash, "-n", str(ROOT / "bootstrap-lab.sh")], check=True)
