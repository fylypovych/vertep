"""Faults seen on a real server: stale version, drain abort, orphan tasks, certificate host."""

import importlib.util
import ipaddress
import json
from urllib.error import HTTPError

import pytest
from cryptography import x509
from fastapi.testclient import TestClient

from core import update_manager
from core.app import app
from core.state import store, task_queue


def test_status_reports_the_running_version_not_the_failed_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(update_manager, "state_root", lambda: tmp_path)
    monkeypatch.setattr(update_manager, "application_version", lambda: "0.0.3.28")
    (tmp_path / "status.json").write_text(json.dumps(
        {"state": "FAILED", "current_version": "0.0.3.26", "available_version": "0.0.3.28", "update_available": True}), encoding="utf-8")
    status = update_manager.update_status()
    assert status["current_version"] == "0.0.3.28" and status["update_available"] is False


def test_drain_survives_a_bad_gateway(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("agent_script", "scripts/update-agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UPDATE_DRAIN_POLL_SECONDS", "0")
    answers = [HTTPError("u", 502, "Bad Gateway", {}, None), {"ready": True}]

    def core_json(path, method="GET"):
        if method == "POST":
            return {}
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(agent, "core_json", core_json)
    state = {"state": "RUNNING", "phase": "CHECKING", "log": []}
    agent.wait_for_drain(tmp_path, state, type("Lease", (), {"assert_current": lambda self: None})())
    assert state["readiness"]["ready"] is True


def test_deleting_a_job_removes_its_queued_tasks():
    client = TestClient(app)
    store.jobs["orphan-job"] = next(iter(store.jobs.values()), None) or _job()
    task = task_queue.enqueue({"job_id": "orphan-job", "task": "voice"})
    monkey = store.delete
    store.delete = lambda job_id: True
    try:
        assert client.delete("/api/jobs/orphan-job").status_code == 200
    finally:
        store.delete = monkey
        store.jobs.pop("orphan-job", None)
    assert not task_queue.has_task(task["task_id"])


def _job():
    return object()


def test_renewed_certificate_covers_the_address_in_use(monkeypatch, tmp_path):
    from services import certificate_service
    monkeypatch.setenv("TLS_ROOT", str(tmp_path))
    monkeypatch.setenv("WEB_DOMAIN", "vertep.local")
    certificate_service.renew("10.10.10.10")
    details = certificate_service.certificate()
    certificate = x509.load_pem_x509_certificate(details["pem"].encode())
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("10.10.10.10") in names.get_values_for_type(x509.IPAddress)
    assert "vertep.local" in names.get_values_for_type(x509.DNSName)
    with pytest.raises(Exception):
        certificate_service.renew("bad host/..")
