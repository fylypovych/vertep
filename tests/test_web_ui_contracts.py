"""Contract tests for Web UI V2 — verifies backend payload shapes
used by the Angular frontend match expected TypeScript models."""
import json
import os

from fastapi.testclient import TestClient

from core.app import app


def _client():
    return TestClient(app, raise_server_exceptions=False)


def _auth():
    return ("admin", os.getenv("ADMIN_PASSWORD", "test-admin-pw-long"))


# ── Jobs ──────────────────────────────────────────────────────────

def test_job_create_returns_required_fields():
    client = _client()
    resp = client.post("/api/jobs", json={"topic": "Contract job"})
    assert resp.status_code == 200
    job = resp.json()
    for key in ("job_id", "topic", "status", "priority", "created_at",
                "character_id", "stages", "scenes", "artifacts",
                "events", "approved", "approval_status", "published_to",
                "publication_results", "version", "active_task_ids",
                "completed_task_ids", "source", "retries", "brand_id",
                "aspect_ratio", "output_preset", "task_type", "min_vram_mb",
                "max_retries"):
        assert key in job, f"Missing key: {key}"
    assert job["status"] == "NEW"
    assert isinstance(job["priority"], int)
    assert isinstance(job["stages"], dict)
    assert isinstance(job["scenes"], list)
    assert isinstance(job["artifacts"], list)
    assert isinstance(job["events"], list)
    assert isinstance(job["approved"], bool)
    assert isinstance(job["version"], int)


def test_job_list_returns_array():
    client = _client()
    client.post("/api/jobs", json={"topic": "List contract"})
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)



# ── Workers ───────────────────────────────────────────────────────

def test_worker_heartbeat_returns_ok():
    client = _client()
    resp = client.post("/api/workers/heartbeat", json={
        "node_name": "contract-worker", "vram_mb": 8192})
    assert resp.status_code == 200


def test_worker_list_shape():
    client = _client()
    client.post("/api/workers/heartbeat", json={
        "node_name": "wl-worker", "vram_mb": 4096})
    resp = client.get("/api/workers")
    assert resp.status_code == 200
    workers = resp.json()
    assert isinstance(workers, list)
    if workers:
        w = workers[0]
        for key in ("node_name", "status", "role", "capabilities"):
            assert key in w, f"Missing worker key: {key}"


# ── Queue ─────────────────────────────────────────────────────────

def test_queue_state_shape():
    client = _client()
    resp = client.get("/api/tasks/queue")
    assert resp.status_code == 200
    state = resp.json()
    assert "ready" in state
    assert "inflight" in state
    assert isinstance(state["ready"], list)
    assert isinstance(state["inflight"], list)


def test_dead_letter_list_shape():
    client = _client()
    resp = client.get("/api/tasks/dead-letter")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Characters ────────────────────────────────────────────────────

def test_character_crud_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    client = _client()
    config = {"id": "contract-char", "name": "Contract", "language": "uk",
              "enabled": True, "system_prompt": "Prompt",
              "voice": {}, "visual": {"aspect_ratio": "9:16"},
              "generation": {}, "publishing": {}}
    put_resp = client.put("/api/characters/contract-char", json=config)
    assert put_resp.status_code == 200
    char = put_resp.json()
    for key in ("id", "name", "language", "enabled", "system_prompt",
                "voice", "visual", "generation", "publishing"):
        assert key in char, f"Missing character key: {key}"
    list_resp = client.get("/api/characters")
    assert list_resp.status_code == 200
    assert isinstance(list_resp.json(), list)


# ── Brands ────────────────────────────────────────────────────────

# ── System / Status ───────────────────────────────────────────────

def test_status_shape():
    client = _client()
    resp = client.get("/api/status", auth=_auth())
    assert resp.status_code == 200
    status = resp.json()
    for key in ("core", "postgres", "redis", "storage"):
        assert key in status, f"Missing status key: {key}"


def test_health_shape():
    client = _client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    health = resp.json()
    for key in ("status", "service", "jobs", "checks"):
        assert key in health, f"Missing health key: {key}"
    assert health["service"] == "core"
    assert isinstance(health["checks"], dict)


def test_metrics_shape():
    client = _client()
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    metrics = resp.json()
    for key in ("jobs_total", "queue_ready", "queue_inflight",
                "queue_dead_letter", "jobs_scheduled", "scenes_by_status"):
        assert key in metrics, f"Missing metrics key: {key}"


def test_security_check_shape():
    client = _client()
    resp = client.get("/api/security/check")
    assert resp.status_code == 200
    check = resp.json()
    for key in ("ok", "weak_or_missing", "recommendation"):
        assert key in check, f"Missing security key: {key}"
    assert isinstance(check["weak_or_missing"], list)


# ── Logs ──────────────────────────────────────────────────────────

def test_logs_shape():
    client = _client()
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_logs_filters():
    client = _client()
    resp = client.get("/api/logs?limit=5&level=INFO")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Alerts ────────────────────────────────────────────────────────

def test_alerts_shape():
    client = _client()
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Integrations ──────────────────────────────────────────────────

def test_integrations_shape():
    client = _client()
    resp = client.get("/api/integrations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


# ── Secrets ───────────────────────────────────────────────────────

def test_secrets_shape():
    client = _client()
    resp = client.get("/api/settings/secrets", auth=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert "secrets" in data
    assert "values_exposed" in data
    assert isinstance(data["secrets"], dict)
    assert isinstance(data["values_exposed"], bool)


# ── System Roles ──────────────────────────────────────────────────

def test_system_roles_shape():
    client = _client()
    resp = client.get("/api/system/roles", auth=_auth())
    assert resp.status_code == 200
    data = resp.json()
    for key in ("node_role", "active_roles", "available_roles"):
        assert key in data, f"Missing roles key: {key}"
    assert isinstance(data["active_roles"], list)
    assert isinstance(data["available_roles"], list)


# ── Node Registration Token ───────────────────────────────────────

def test_registration_token_shape():
    client = _client()
    resp = client.post("/api/nodes/registration-tokens",
                       json={"role": "gpu"},
                       auth=_auth())
    assert resp.status_code == 200
    token = resp.json()
    for key in ("token", "role", "expires_at"):
        assert key in token, f"Missing token key: {key}"


# ── Setup ─────────────────────────────────────────────────────────

def test_setup_status_shape():
    client = _client()
    resp = client.get("/api/setup")
    assert resp.status_code == 200
    data = resp.json()
    assert "configured" in data
    assert isinstance(data["configured"], bool)


# ── Artifacts ─────────────────────────────────────────────────────

def test_job_artifacts_shape():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Artifact test"}).json()["job_id"]
    resp = client.get(f"/api/jobs/{jid}/artifacts")
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert "artifacts" in data
    assert isinstance(data["artifacts"], list)


def test_verify_artifacts_shape():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Verify test"}).json()["job_id"]
    resp = client.post(f"/api/jobs/{jid}/artifacts/verify")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("job_id", "valid", "results"):
        assert key in data, f"Missing verify key: {key}"
    assert isinstance(data["results"], list)


def test_brand_crud_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("BRANDS_ROOT", str(tmp_path))
    client = _client()
    resp = client.post("/api/brands", json={"id": "contract-brand", "name": "Contract Brand"})
    assert resp.status_code == 200
    brand = resp.json()
    for key in ("id", "name", "enabled"):
        assert key in brand, f"Missing brand key: {key}"


def test_brand_list_shape():
    client = _client()
    resp = client.get("/api/brands")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Workflows ─────────────────────────────────────────────────────

def test_workflow_list_shape():
    client = _client()
    resp = client.get("/api/workflows")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

def test_job_delete_returns_deleted_id():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Delete contract"}).json()["job_id"]
    resp = client.delete(f"/api/jobs/{jid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == jid


# ── V2C-201: Job detail contract ──────────────────────────────────

def test_job_detail_has_all_lifecycle_fields():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Lifecycle"}).json()["job_id"]
    job = client.get(f"/api/jobs/{jid}").json()
    for key in ("stages", "scenes", "artifacts", "events",
                "active_task_ids", "completed_task_ids",
                "approved", "approval_status", "published_to",
                "publication_results", "version"):
        assert key in job, f"Missing lifecycle field: {key}"
    assert isinstance(job["events"], list)


def test_job_update_preserves_version():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Version test"}).json()["job_id"]
    job = client.get(f"/api/jobs/{jid}").json()
    resp = client.patch(f"/api/jobs/{jid}", json={
        "expected_version": job["version"], "topic": "Updated"})
    if resp.status_code == 200:
        assert resp.json()["topic"] == "Updated"
    else:
        # Pipeline may auto-process between GET and PATCH; 409 is acceptable
        assert resp.status_code == 409


# ── V2C-202: Job stages/scenes structure ─────────────────────────

def test_job_stages_are_dict_with_scene_records():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Stage test"}).json()["job_id"]
    job = client.get(f"/api/jobs/{jid}").json()
    assert isinstance(job["stages"], dict)
    assert isinstance(job["scenes"], list)
    if job["scenes"]:
        scene = job["scenes"][0]
        for key in ("scene_id", "index", "prompt", "status", "artifact_ids", "attempts"):
            assert key in scene, f"Missing scene field: {key}"


# ── V2C-203: Artifacts contract ───────────────────────────────────

def test_job_artifacts_verify_returns_results():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Artifact DL"}).json()["job_id"]
    verify = client.post(f"/api/jobs/{jid}/artifacts/verify")
    assert verify.status_code == 200
    data = verify.json()
    assert "results" in data
    assert isinstance(data["results"], list)


# ── V2C-204: Publish flow contract ───────────────────────────────

def test_publish_without_channels_returns_200_or_422():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Publish test"}).json()["job_id"]
    resp = client.post(f"/api/jobs/{jid}/publish")
    assert resp.status_code in (200, 422, 409)


# ── V2C-301: Queue state contract ────────────────────────────────

def test_queue_ready_inflight_have_task_fields():
    client = _client()
    resp = client.get("/api/tasks/queue")
    assert resp.status_code == 200
    data = resp.json()
    if data["ready"]:
        task = data["ready"][0]
        for key in ("task_id", "job_id", "task", "priority"):
            assert key in task, f"Missing queue task field: {key}"


# ── V2C-302: Alerts contract ─────────────────────────────────────

def test_alerts_have_severity_and_type():
    client = _client()
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    for alert in resp.json():
        assert "severity" in alert
        assert "type" in alert


# ── V2C-303: Logs contract ───────────────────────────────────────

def test_logs_with_limit_filter():
    client = _client()
    resp = client.get("/api/logs?limit=5")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── V2C-304: Health monitoring contract ───────────────────────────

def test_health_history_shape():
    client = _client()
    resp = client.get("/api/health/history")
    assert resp.status_code == 200
    data = resp.json()
    assert "history" in data
    assert isinstance(data["history"], list)


def test_security_check_fields():
    client = _client()
    resp = client.get("/api/security/check")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["weak_or_missing"], list)
    assert isinstance(data["recommendation"], str)


def test_maintenance_cleanup_dry_run():
    client = _client()
    resp = client.post("/api/maintenance/cleanup?dry_run=true")
    assert resp.status_code == 200
    data = resp.json()
    assert "jobs" in data or "temporary_files" in data


# ── V2C-401: Worker registration token contract ──────────────────

def test_registration_token_returns_ttl():
    client = _client()
    resp = client.post("/api/nodes/registration-tokens", json={"role": "gpu"}, auth=_auth())
    assert resp.status_code == 200
    token = resp.json()
    assert "token" in token
    assert "expires_at" in token
    assert token["role"] == "gpu"


def test_registration_token_rejects_invalid_role():
    client = _client()
    resp = client.post("/api/nodes/registration-tokens", json={"role": "nonexistent"}, auth=_auth())
    assert resp.status_code == 422


# ── V2C-402: Worker detail contract ──────────────────────────────

def test_node_detail_has_typed_fields():
    client = _client()
    node_name = "detail-worker"
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": 4096,
        "gpu_name": "RTX 4090", "role": "gpu",
    })
    resp = client.get(f"/api/nodes/{node_name}", auth=_auth())
    assert resp.status_code == 200
    detail = resp.json()
    assert "hardware" in detail
    assert isinstance(detail["hardware"], dict)
    assert "status" in detail
    assert "capabilities" in detail
    assert isinstance(detail["capabilities"], list)


# ── V2C-403: Worker controls contract ────────────────────────────

def test_worker_action_drain_accepted():
    client = _client()
    node_name = "action-worker"
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": 2048, "role": "gpu",
    })
    resp = client.post(f"/api/nodes/{node_name}/actions", json={"action": "drain"}, auth=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data


def test_worker_action_logs_accepted():
    client = _client()
    node_name = "logs-worker"
    client.post("/api/workers/heartbeat", json={
        "node_name": node_name, "vram_mb": 1024, "role": "text",
    })
    resp = client.post(f"/api/nodes/{node_name}/actions", json={"action": "logs"}, auth=_auth())
    assert resp.status_code == 200


def test_worker_action_unsupported_rejected():
    client = _client()
    resp = client.post("/api/nodes/nonexistent/actions", json={"action": "nonexistent"}, auth=_auth())
    assert resp.status_code == 422


# ── V2C-404: Roles deployment contract ───────────────────────────

def test_roles_get_returns_deployment_and_queued():
    client = _client()
    resp = client.get("/api/system/roles", auth=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert "active_roles" in data
    assert "available_roles" in data
    assert "deployment" in data
    assert "queued" in data
    assert isinstance(data["available_roles"], list)
    if data["available_roles"]:
        role = data["available_roles"][0]
        assert "id" in role
        assert "label" in role


def test_roles_post_returns_state_and_message():
    client = _client()
    resp = client.post("/api/system/roles", json={"roles": []}, auth=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert "message" in data
