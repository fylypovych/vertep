"""Actual CORE/Worker process and controlled Ollama HTTP workflow execution."""

import base64
import json
import os
import subprocess
import sys

import pytest

from tests.test_text_redis_process import contour, heartbeat, ROOT, TOKEN


@pytest.fixture
def workflow_contour(tmp_path, monkeypatch, request):
    definitions = tmp_path / "dag"
    definitions.mkdir()
    (definitions / "configured.json").write_text(json.dumps({"name": "Configured", "version": "1.0.0", "tasks": [
        {"id": "script", "type": "text_generation", "approval_required": True},
        {"id": "revision", "type": "text_generation", "dependencies": ["script"]},
    ]}), encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_DEFINITIONS_ROOT", str(definitions))
    monkeypatch.setenv("WORKFLOW_STATE_ROOT", str(tmp_path / "dag-state"))
    yield from contour.__wrapped__(tmp_path, monkeypatch, request)


def test_core_worker_http_dispatch_approval_restart(workflow_contour):
    context = workflow_contour
    api = context["api"]
    heartbeat(api)
    response = api.post("/api/jobs", json={"topic": "Real workflow", "character_id": "processchar", "workflow_template": "configured"})
    assert response.status_code == 200, response.text
    job = response.json()["job_id"]

    def worker():
        process = subprocess.run([sys.executable, "tests/workflow_worker_process.py", str(api.base_url).rstrip("/")],
            cwd=ROOT, env={**os.environ, "NODE_API_TOKEN": TOKEN, "NODE_ROLE": "text"},
            capture_output=True, text=True, timeout=35)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout.splitlines()[-1])

    first = worker()
    assert first["success"] and first["dependencies"] == []
    status = api.get(f"/api/workflow-execution/{job}").json()
    artifacts = status["tasks"]["script"]["output"]
    assert json.loads(base64.b64decode(artifacts[0]["data_base64"]))["title"] == "Controlled Text output"
    assert status["tasks"]["revision"]["status"] == "PENDING"
    assert len(context["requests"]) == 1
    # Wrong lease and incompatible protocol cannot mutate execution state.
    stale = api.post("/api/tasks/result/workflow", json={"node_name": "text-process", "task_id": first["task_id"], "lease_id": "wrong", "result": []})
    assert stale.status_code == 409
    assert api.post("/api/tasks/claim/workflow", json={"node_name": "text-process", "protocol_version": "2.0.0"}).status_code == 409
    context["restart"]("core")
    restored = api.get(f"/api/workflow-execution/{job}").json()
    assert restored["tasks"]["script"]["output"] == artifacts
    heartbeat(api)
    approved = api.post(f"/api/workflow-execution/{job}/action", json={"action": "approve", "task_id": "script", "expected_revision": 1})
    assert approved.status_code == 200, approved.text
    second = worker()
    assert second["success"] and second["dependencies"] == ["script"]
    completed = api.get(f"/api/workflow-execution/{job}").json()
    assert completed["status"] == "COMPLETED"
    assert len(context["requests"]) == 2


def test_workflow_redis_document_survives_redis_restart(workflow_contour):
    from core.workflow.database_state import DatabaseWorkflowState
    context = workflow_contour
    options = context["redis"].connection_pool.connection_kwargs
    url = f"redis://127.0.0.1:{options['port']}/0"
    state = DatabaseWorkflowState("redis", url)
    state.save_job("durable", {"tasks": {"x": {"output": "actual", "approval": "APPROVED"}}, "status": "PAUSED"})
    context["restart"]("redis")
    fresh = DatabaseWorkflowState("redis", url)
    assert fresh.load_job("durable")["tasks"]["x"]["output"] == "actual"
    assert "durable" in fresh.list_checkpoints()


def test_protocol_progress_cancel_and_late_result(workflow_contour):
    api = workflow_contour["api"]
    heartbeat(api)
    created = api.post("/api/jobs", json={"topic": "cancel", "workflow_template": "configured"})
    assert created.status_code == 200, created.text
    job = created.json()["job_id"]
    claimed = api.post("/api/tasks/claim/workflow", json={"node_name": "text-process"})
    assert claimed.status_code == 200, claimed.text
    task = claimed.json()["task"]
    message = {"node_name": "text-process", "task_id": task["task_id"], "lease_id": task["lease_id"]}
    renewed = api.post("/api/tasks/renew/workflow", json={**message, "progress_percent": 42})
    assert renewed.status_code == 200 and renewed.json()["renewed"]
    assert api.get(f"/api/workflow-execution/{job}").json()["tasks"]["script"]["progress_percent"] == 42
    assert api.post(f"/api/workflow-execution/{job}/action", json={"action": "cancel"}).status_code == 200
    renewed = api.post("/api/tasks/renew/workflow", json=message)
    assert renewed.json()["cancelled"]
    late = api.post("/api/tasks/result/workflow", json={**message, "result": {"late": "must not be accepted"}})
    assert late.status_code == 409
    status = api.get(f"/api/workflow-execution/{job}").json()
    assert status["status"] == "CANCELLED" and status["tasks"]["script"]["output"] is None


def test_signed_module_discovery_dispatch_and_uninstall(workflow_contour, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from core.module.packages import ModulePackages
    from worker.module_runtime import inventory
    from datetime import datetime, timezone
    context = workflow_contour
    api = context["api"]
    key = Ed25519PrivateKey.generate()
    trust = context["root"] / "trust"
    trust.mkdir()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    (trust / "owner.pem").write_bytes(public)
    root = context["root"] / "modules"
    monkeypatch.setenv("VERTEP_MODULE_ROOT", str(root))
    monkeypatch.setenv("VERTEP_MODULE_TRUST_ROOT", str(trust))
    source = b"import base64\ndef execute(value): return [{'filename':'module.txt','kind':'text','data_base64':base64.b64encode(value['text'].upper().encode()).decode()}]\ndef health(): return {'ready':True}\ndef resource_query(): return {'cpu_cores':1}\ndef cancel(task_id): return {'cancelled':False}\n"
    content = {"manifest": {"name": "example", "version": "1.0.0", "capabilities": ["text_generation"], "resource_requirements": {"min_cpu_cores": 1}},
               "files": {"module.py": base64.b64encode(source).decode()}}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    package = {**content, "signer": "owner", "signature": base64.b64encode(key.sign(raw)).decode()}
    manager = ModulePackages(root, {"owner": public})
    manager.install(package)
    (context["root"] / "dag/module.json").write_text(json.dumps({"name": "Module", "tasks": [{"id": "module", "type": "custom", "module": "example", "input": {"text": "actual module"}}]}))
    def report():
        response = api.post("/api/workers/heartbeat", json={"node_name": "text-process", "role": "text", "status": "FREE", "cpu_cores": 2,
            "capabilities": ["text_generation"], "module_inventory": inventory(),
            "self_test": {"status": "PASSED", "role": "text", "checked_at": datetime.now(timezone.utc).isoformat()}})
        assert response.status_code == 200, response.text
    report()
    response = api.post("/api/jobs", json={"topic": "module", "workflow_template": "module"})
    assert response.status_code == 200, response.text
    job = response.json()["job_id"]
    process = subprocess.run([sys.executable, "tests/workflow_worker_process.py", str(api.base_url).rstrip("/")], cwd=ROOT,
        env={**os.environ, "NODE_API_TOKEN": TOKEN, "NODE_ROLE": "text"}, capture_output=True, text=True, timeout=35)
    assert process.returncode == 0, process.stderr
    completed = api.get(f"/api/workflow-execution/{job}").json()
    assert completed["status"] == "COMPLETED"
    assert base64.b64decode(completed["tasks"]["module"]["output"][0]["data_base64"]) == b"ACTUAL MODULE"
    manager.uninstall("example")
    assert inventory() == []
    report()
    # Registration of the changed inventory is performed on the stock poll path.
    assert api.post("/api/tasks/claim/workflow", json={"node_name": "text-process"}).status_code == 200
    second = api.post("/api/jobs", json={"topic": "removed", "workflow_template": "module"}).json()["job_id"]
    missing = api.get(f"/api/workflow-execution/{second}").json()
    assert missing["tasks"]["module"]["status"] == "PENDING"
    assert "not discovered" in missing["tasks"]["module"]["error"]


def test_legacy_generation_retains_its_worker_pipeline(workflow_contour):
    from tests.test_text_redis_process import create, claim, result, get_job
    api = workflow_contour["api"]
    heartbeat(api)
    job_id = create(api)
    legacy = get_job(api, job_id)
    workflow_id = legacy["workflow_execution_id"]
    assert workflow_id
    issued = claim(api)
    assert issued and not issued.get("architecture_workflow")
    response, artifacts = result(api, issued)
    assert response.status_code == 200, response.text
    assert artifacts[0]["kind"] == "script"
    legacy = get_job(api, job_id)
    assert legacy["status"] == "SCRIPT_PENDING_APPROVAL"
    observed = api.get(f"/api/workflow-execution/{workflow_id}").json()
    assert list(observed["tasks"]) == ["generation"]
    assert observed["tasks"]["generation"]["output"]["job_id"] == job_id
    assert observed["tasks"]["generation"]["output"]["status"] == legacy["status"]
    assert api.post("/api/tasks/claim/workflow", json={"node_name": "text-process"}).json()["task"] is None


def test_cancel_result_race_never_acknowledges_a_fenced_output(workflow_contour):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    api = workflow_contour["api"]
    heartbeat(api)
    for _ in range(5):
        job = api.post("/api/jobs", json={"topic": "race", "workflow_template": "configured"}).json()["job_id"]
        task = api.post("/api/tasks/claim/workflow", json={"node_name": "text-process"}).json()["task"]
        assert task is not None
        gate = Barrier(2)
        def complete():
            gate.wait(timeout=3)
            return api.post("/api/tasks/result/workflow", json={"node_name": "text-process", "task_id": task["task_id"],
                "lease_id": task["lease_id"], "result": {"external": "output"}})
        def cancel():
            gate.wait(timeout=3)
            return api.post(f"/api/workflow-execution/{job}/action", json={"action": "cancel"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            completed, cancelled = pool.submit(complete), pool.submit(cancel)
            reply = completed.result(timeout=10)
            assert cancelled.result(timeout=10).status_code == 200
        status = api.get(f"/api/workflow-execution/{job}").json()
        assert reply.status_code in {200, 409}, reply.text
        if reply.status_code == 200:
            assert status["tasks"]["script"]["output"] == {"external": "output"}
        else:
            assert status["tasks"]["script"]["output"] is None
