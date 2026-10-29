"""Checkpoint→backup CLI→mutation→restore→CORE restart→actual Worker resume."""

import json
import os
import subprocess
import sys

import pytest

from tests.test_workflow_http_process import workflow_contour
from tests.test_text_redis_process import ROOT, TOKEN, heartbeat


@pytest.fixture
def migration_contour(tmp_path, monkeypatch, request):
    monkeypatch.setenv("VERTEP_TEXT_MODULE_ENABLED", "true")
    monkeypatch.setenv("MODULE_STATE_ROOT", str(tmp_path / "module-state"))
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "workflow-backups"))
    yield from workflow_contour.__wrapped__(tmp_path, monkeypatch, request)


def test_backup_cli_process_resume_preserves_approval_artifacts_and_module_state(migration_contour):
    context = migration_contour
    api, root = context["api"], context["root"]
    heartbeat(api)
    created = api.post("/api/jobs", json={"topic": "checkpoint", "workflow_template": "configured", "owner": "owner", "project": "project", "acl": ["owner"]})
    assert created.status_code == 200, created.text
    job = created.json()["job_id"]
    def worker():
        result = subprocess.run([sys.executable, "tests/workflow_worker_process.py", str(api.base_url).rstrip("/")], cwd=ROOT,
            env={**os.environ, "NODE_API_TOKEN": TOKEN, "NODE_ROLE": "text"}, capture_output=True, text=True, timeout=35)
        assert result.returncode == 0, result.stderr
    worker()
    scrape = api.get("/metrics")
    assert scrape.status_code == 200
    assert 'vertep_module_successes{node="text-process",module="text"} 1.0' in scrape.text
    overview = api.get("/api/metrics").json()["module_overview"]["text-process"]
    assert next(module for module in overview if module["name"] == "text")["metrics"]["executions"] == 1
    before = api.get(f"/api/workflow-execution/{job}").json()
    assert before["tasks"]["script"]["output"]
    assert api.post(f"/api/workflow-execution/{job}/action", json={"action": "pause"}).status_code == 200
    assert api.post(f"/api/workflow-execution/{job}/action", json={"action": "approve", "task_id": "script"}).status_code == 200
    def cli(*args):
        result = subprocess.run([sys.executable, "scripts/vertep-cli.py", "backup", *args], cwd=ROOT, env=os.environ.copy(),
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return result.stdout
    snapshot = cli("create", "--version", "0.0.2.89").strip().split()[-1]
    assert snapshot in cli("list")
    assert "valid" in cli("verify", snapshot).lower() or "verified" in cli("verify", snapshot).lower()
    context["stop"]("core")
    state_file = root / "dag-state" / f"{job}.json"
    document = json.loads(state_file.read_text())
    assert (document["owner"], document["project"], document["acl"]) == ("owner", "project", ["owner"])
    module_file = root / "module-state/text/state.json"
    saved_module = module_file.read_bytes()
    document["tasks"]["script"]["output"] = "mutated"
    state_file.write_text(json.dumps(document))
    module_file.write_text('{"mutated":true}')
    cli("restore", snapshot)
    assert module_file.read_bytes() == saved_module
    context["start"]("core")
    restored = api.get(f"/api/workflow-execution/{job}").json()
    assert restored["tasks"]["script"]["output"] == before["tasks"]["script"]["output"]
    assert restored["tasks"]["script"]["approval"] == "APPROVED"
    heartbeat(api)
    assert api.post(f"/api/workflow-execution/{job}/action", json={"action": "resume"}).status_code == 200
    worker()
    final = api.get(f"/api/workflow-execution/{job}").json()
    assert final["status"] == "COMPLETED"
    assert len(context["requests"]) == 2
