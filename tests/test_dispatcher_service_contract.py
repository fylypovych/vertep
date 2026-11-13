"""CORE selects the same worker with and without the isolated dispatcher service."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from core.api import job_helpers
from core.models import Job
from services.dispatcher_service import app


def _worker(name: str, capabilities: list[str], vram: int = 0) -> dict:
    return {"node_name": name, "status": "READY", "capabilities": capabilities,
            "vram_mb": vram, "free_vram_mb": vram,
            "last_seen": datetime.now(timezone.utc).isoformat()}


def _job() -> Job:
    return Job.model_validate({
        "job_id": "job", "topic": "topic", "character_id": "character", "priority": 1,
        "status": "NEW", "task_type": "image", "min_vram_mb": 6000,
        "created_at": datetime.now(timezone.utc).isoformat()})


@pytest.mark.parametrize("worker,task_type,min_vram_mb,expected", [
    (_worker("text", ["text_generation"]), "text", 0, "text"),
    (_worker("voice", ["speech_synthesis"]), "voice", 0, "voice"),
    (_worker("publisher", ["publishing"]), "publish", 0, "publisher"),
    (_worker("assembly", ["video_assembly"]), "assembly", 0, "assembly"),
    (_worker("small-gpu", ["image_generation"], 2048), "image", 2000, "small-gpu"),
    (_worker("gpu", ["image_generation"], 8192), "text", 0, None),
    (_worker("gpu", ["image_generation"], 8192), "image", 20000, None),
])
def test_service_selection_matches_in_process_selection(monkeypatch, worker, task_type, min_vram_mb, expected):
    monkeypatch.delenv("REQUIRE_WORKER_SELF_TEST", raising=False)
    # Parametrized at collection: refresh the heartbeat or a long run makes the worker stale.
    worker = {**worker, "last_seen": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setenv("INTERNAL_API_KEY", "dispatch-key")
    client = TestClient(app)
    monkeypatch.setattr(job_helpers.common.httpx, "post",
                        lambda url, json, headers, timeout: client.post("/select", json=json, headers=headers))

    monkeypatch.delenv("DISPATCHER_URL", raising=False)
    local = job_helpers._select_worker([dict(worker)], _job(), task_type, min_vram_mb)
    monkeypatch.setenv("DISPATCHER_URL", "http://dispatcher")
    remote = job_helpers._select_worker([dict(worker)], _job(), task_type, min_vram_mb)

    assert (local or {}).get("node_name") == expected
    assert (remote or {}).get("node_name") == expected


def test_service_applies_voice_requirements(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "dispatch-key")
    worker = {**_worker("voice", ["speech_synthesis"]), "voice_catalog": {"voices": ["known"]}}
    response = TestClient(app).post("/select", headers={"x-vertep-internal-key": "dispatch-key"}, json={
        "job": _job().model_dump(mode="json"), "workers": [worker], "task_type": "voice", "min_vram_mb": 0,
        "voice_requirements": {"voice": "missing"}})

    assert response.status_code == 200
    assert response.json()["worker"] is None


def test_production_dispatcher_enforces_worker_self_test():
    compose = yaml.safe_load(Path("deploy/docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["dispatcher"]["environment"]["REQUIRE_WORKER_SELF_TEST"] == "true"


@pytest.mark.parametrize("configured,sent", [("", ""), ("dispatch-key", ""), ("dispatch-key", "wrong")])
def test_service_refuses_a_call_without_the_internal_key(monkeypatch, configured, sent):
    """I-12: only CORE holding INTERNAL_API_KEY may ask for a selection; no key configured = refused."""
    monkeypatch.setenv("INTERNAL_API_KEY", configured)
    response = TestClient(app).post("/select", headers={"x-vertep-internal-key": sent},
                                    json={"job": _job().model_dump(mode="json"), "workers": []})
    assert response.status_code == 401
    assert TestClient(app).get("/health").status_code == 200
