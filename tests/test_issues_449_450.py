"""Automated API/executor contracts; subprocess results are simulated, not live deployment proof."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import health_checks as hc
from core.api import observability, system
from core.deployment_plan import create_plan
from core.deployment_state import operation_state
from core.node_registry import node_roles
from core.version import application_version


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.delenv("NODE_ADDITIONAL_ROLES", raising=False)
    monkeypatch.delenv("CORE_ADDRESS", raising=False)
    monkeypatch.setattr(hc, "check_docker", lambda: (True, "health_probe_ok"))
    monkeypatch.setattr(hc, "check_postgres", lambda: (True, "ok"))
    monkeypatch.setattr(hc, "check_redis", lambda: (True, "ok"))
    for name in ["gpu", "cuda", "comfyui", "gpu_worker", "tts", "publisher", "monitoring", "backup"]:
        monkeypatch.setattr(hc, "check_" + name, lambda: (False, "health_endpoint_unavailable"))
    monkeypatch.setattr(hc, "check_ollama", lambda url=None: (False, "health_endpoint_unavailable"))
    monkeypatch.setattr(observability, "_run_health_checks", hc.run_checks)
    monkeypatch.setattr(observability, "_health_status", hc.health_status)
    monkeypatch.setattr(system, "update_status", lambda: {})
    (tmp_path / "installation.json").write_text(json.dumps({"completed_at": "now", "node_role": "core", "ai_backend": {"type": "skip"}}))
    app = FastAPI()
    app.include_router(observability.router)
    app.include_router(system.router)
    return TestClient(app), tmp_path


def save_plan(root, extras):
    plan = create_plan(node_roles(), "core", application_version(), extras)
    (root / "deployment-plan.json").write_text(json.dumps(plan))
    return plan


@pytest.mark.parametrize("role", ["gpu", "voice", "publisher", "text", "backup", "monitoring"])
def test_history_keeps_failures_and_new_unselected_roles_do_not_fail(api, role):
    client, root = api
    save_plan(root, [role])
    assert client.get("/api/health").json()["status"] == "UNHEALTHY"
    save_plan(root, [])
    current = client.get("/api/health").json()
    assert current["status"] == "HEALTHY"
    assert current["checks"]["selected_roles"] == ["core"]
    assert hc.health_status(current["checks"]) == "HEALTHY"  # JSON list tuples
    history = client.get("/api/health/history").json()["history"]
    # Only unhealthy events are journaled; the recorded failure keeps its own selection.
    assert [entry["status"] for entry in history] == ["UNHEALTHY"]
    assert role in history[0]["checks"]["selected_roles"]


def test_history_skips_partial_line_without_losing_valid_failures(api):
    client, root = api
    path = root / "update/health-history.jsonl"
    path.parent.mkdir()
    record = {"status": "UNHEALTHY", "checks": {"gpu": [False, "past failure"]}}
    path.write_text(json.dumps(record) + '\n{"partial":\n')
    assert client.get("/api/health/history?limit=-1").json()["history"] == [record]
    assert client.get("/api/health/history").json()["history"] == [record]


def _event(moment, status="UNHEALTHY", gpu=(False, "health_gpu_unavailable")):
    return {"timestamp": moment.isoformat(), "role": "core", "status": status,
            "checks": {"role": "core", "required": ["docker", "gpu"], "selected_roles": ["core", "gpu"],
                       "docker": [True, "ok"], "gpu": list(gpu)}}


def test_only_unhealthy_events_are_journaled_and_polling_is_one_incident(tmp_path):
    from datetime import datetime, timedelta, timezone
    from core import health_history as hh
    start = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)
    hh.record(tmp_path, _event(start, "HEALTHY", (True, "ok")), start)
    assert not (tmp_path / hh.CURRENT).exists()
    first = [_event(start + timedelta(seconds=10 * i)) for i in range(30)]  # 5 minutes of polling
    second = _event(start + timedelta(hours=2))  # the same failure again after a recovery
    other = _event(start + timedelta(hours=2, seconds=10), gpu=(False, "health_gpu_runtime_failed"))
    for item in [*first, second, other]:
        hh.record(tmp_path, item, start)
        hh.record(tmp_path, _event(start, "HEALTHY", (True, "ok")), start)
    lines = hh.read_lines(tmp_path, start)
    assert len(lines) == 32 and all(json.loads(line)["status"] == "UNHEALTHY" for line in lines)
    assert hh.incidents(lines, 10) == [first[0], second, other]
    assert hh.incidents(lines, 1) == [other]


def test_daily_rotation_destroys_older_day_and_migrates_legacy_journal(tmp_path):
    from datetime import datetime, timedelta, timezone
    from core import health_history as hh
    day = datetime(2026, 11, 12, 23, 59, tzinfo=timezone.utc)
    legacy = [_event(day, "HEALTHY", (True, "ok")), _event(day)]
    (tmp_path / hh.CURRENT).write_text("\n".join(json.dumps(x) for x in legacy) + '\n{"partial":\n')
    assert hh.read_lines(tmp_path, day) == [json.dumps(legacy[1])]  # healthy spam is not kept
    assert not (tmp_path / hh.CURRENT).exists()
    hh.record(tmp_path, _event(day), day)
    # Next day: today's file becomes the previous day, the migrated older one is destroyed.
    tomorrow = day + timedelta(minutes=2)
    hh.record(tmp_path, _event(tomorrow), tomorrow)
    lines = hh.read_lines(tmp_path, tomorrow)
    assert [json.loads(line)["timestamp"] for line in lines] == [day.isoformat(), tomorrow.isoformat()]
    # Without events for more than a day, nothing older than the previous day survives.
    later = tomorrow + timedelta(days=3)
    assert hh.read_lines(tmp_path, later) == []
    assert not (tmp_path / hh.PREVIOUS).exists() and not (tmp_path / hh.CURRENT).exists()
    assert (tmp_path / hh.STAMP).read_text() == later.date().isoformat()


def test_history_api_returns_incidents_of_unhealthy_journal(api):
    client, root = api
    save_plan(root, ["gpu"])
    for _ in range(3):
        assert client.get("/api/health").json()["status"] == "UNHEALTHY"
    save_plan(root, [])
    for _ in range(3):
        assert client.get("/api/health").json()["status"] == "HEALTHY"
    assert len(client.get("/api/health/history").json()["history"]) == 3
    incidents = client.get("/api/health/history?changes=10").json()["history"]
    assert [item["status"] for item in incidents] == ["UNHEALTHY"]
    assert "gpu" in incidents[0]["checks"]["selected_roles"]


@pytest.mark.parametrize("roles", [["voice"], ["publisher"], ["voice", "publisher"]])
def test_request_reload_idempotency_and_conflict(api, roles):
    client, root = api
    save_plan(root, [])
    assert client.post("/api/system/roles", json={"roles": roles}).json()["state"] == "QUEUED"
    path = root / "deployment-request.json"
    original = path.read_bytes()
    request = json.loads(original)
    assert request["request_id"] and request["requested_at"]
    assert client.post("/api/system/roles", json={"roles": roles}).json()["state"] == "QUEUED"
    assert path.read_bytes() == original
    assert client.post("/api/system/roles", json={"roles": ["text"]}).status_code == 409
    status = client.get("/api/system/roles").json()
    assert status["requested_roles"] == sorted(roles) and status["active_roles"] == []
    assert status["deployment"]["state"] == "QUEUED"
    save_plan(root, roles)  # executor stages its target plan before verification
    (root / "deployment-status.json").write_text(json.dumps({"state": "APPLYING", "request_id": request["request_id"], "previous_roles": [], "additional_roles": roles}))
    assert client.get("/api/system/roles").json()["active_roles"] == []


@pytest.mark.parametrize("state,seconds", [("QUEUED", 121), ("APPLYING", 14401)])
def test_stalled_executor_keeps_actual_state_and_diagnoses(state, seconds, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    request = {"request_id": "new", "requested_at": old, "additional_roles": ["voice"]}
    status = {"request_id": "new", "state": state, "updated_at": old}
    result = operation_state(request, status if state == "APPLYING" else {}, tmp_path / "request.json")
    assert result["stalled"] and result["message"]
    assert result["state"] == state
    assert "progress" not in result


@pytest.mark.parametrize("extras", [[], ["voice"], ["publisher"], ["voice", "publisher"], ["text", "backup", "monitoring"]])
@pytest.mark.parametrize("failure", [None, "pull", "up", "health", "prerequisite"])
def test_executor_terminal_results_and_real_phase_boundaries(tmp_path, monkeypatch, extras, failure):
    spec = importlib.util.spec_from_file_location("deployment_450", Path(__file__).parents[1] / "scripts/apply-deployment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("UPDATE_DATABASE_URL", raising=False)
    root = tmp_path
    config = root / "config"
    config.mkdir()
    catalog = node_roles()
    plan = create_plan(catalog, "core", "0.0.4.0", extras)
    (root / ".env").write_text("VERTEP_VERSION=0.0.4.0\n")
    (config / "node_roles.json").write_text(json.dumps(catalog))
    request = {"role": "core", "version": "0.0.4.0", "additional_roles": extras,
               "plan_sha256": "bad" if failure == "prerequisite" else plan["sha256"], "request_id": "op", "ai_backend": "skip"}
    (config / "deployment-request.json").write_text(json.dumps(request))
    calls, phases = [], []
    real_atomic = module.atomic_json
    def atomic(path, value):
        if path.name == "deployment-status.json":
            phases.append((value["state"], value.get("phase")))
        real_atomic(path, value)
    monkeypatch.setattr(module, "atomic_json", atomic)
    monkeypatch.setattr(module, "ensure_persistent_user_data_legacy_migration", lambda: None)
    monkeypatch.setattr(module, "migrate_staged_legacy_roots", lambda path: None)
    def runner(command, **kwargs):
        calls.append(command)
        if failure in {"pull", "up"} and failure in command:
            raise RuntimeError("fixture " + failure + " failure")
        if "ps" in command:
            rows = [{"Service": service, "State": "exited" if service == "migrate" else "running",
                     "ExitCode": 0, "Health": "unhealthy" if failure == "health" else "healthy"}
                    for service in plan["services"]]
            return SimpleNamespace(stdout="\n".join(json.dumps(row) for row in rows), returncode=0)
        return SimpleNamespace(stdout="", returncode=1)
    if failure:
        with pytest.raises(RuntimeError):
            module.apply(root, runner)
    else:
        assert module.apply(root, runner)["state"] == "SUCCEEDED"
        assert ("APPLYING", "PULLING") in phases and ("APPLYING", "VERIFYING") in phases
        up = next(command for command in calls if "up" in command)
        assert set(plan["services"]) <= set(up)
        assert "comfyui" not in up
    status = json.loads((config / "deployment-status.json").read_text())
    assert status["state"] == ("FAILED" if failure else "SUCCEEDED")
    assert status["request_id"] == "op"
    assert not (config / "deployment-request.json").exists()
    if failure:
        assert status["error"]


def test_competing_executor_cannot_consume_or_fail_the_owners_request(tmp_path, monkeypatch):
    from core.update_lease import UpdateLease
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("UPDATE_DATABASE_URL", raising=False)
    spec = importlib.util.spec_from_file_location("deployment_lock_450", Path(__file__).parents[1] / "scripts/apply-deployment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "config"
    config.mkdir()
    request = config / "deployment-request.json"
    request.write_text('{"request_id":"owner"}')
    original = request.read_bytes()
    with UpdateLease(config / "update", "owner"):
        with pytest.raises(RuntimeError, match="lease"):
            module.apply(tmp_path)
    assert request.read_bytes() == original
    assert not (config / "deployment-status.json").exists()


@pytest.mark.parametrize("role,probe", [("voice", "TTS_HEALTH_URL"), ("publisher", "PUBLISHER_HEALTH_URL")])
def test_local_role_readiness_uses_current_http_result_not_saved_plan(tmp_path, monkeypatch, role, probe):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from core import role_runtime
    response = {"code": 503}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(response["code"])
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setenv("NODE_ADDITIONAL_ROLES", "gpu")  # persisted plan supersedes old env
    monkeypatch.delenv("CORE_ADDRESS", raising=False)
    monkeypatch.setenv(probe, f"http://127.0.0.1:{server.server_port}/health")
    monkeypatch.setattr(hc, "check_docker", lambda: (True, "ok"))
    plan = save_plan(tmp_path, [role])
    (tmp_path / "runtime-inventory.json").write_text(json.dumps({"containers": [
        {"service": service, "state": "running", "health": "healthy"} for service in plan["services"]]}))
    try:
        assert role_runtime.role_runtime_status(role)["runtime_status"] == "DEGRADED"
        response["code"] = 200
        assert role_runtime.role_runtime_status(role)["runtime_status"] == "READY"
        assert role_runtime._additional_roles() == {role}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
