"""Contract tests for Vertep Web UI V2 job workspace (issue i.0.0.0.21).

Verifies the backend payloads used by the Angular job lifecycle UI:

- structured timeline / events store (no line parsing, no fabricated timestamps);
- server-side job filters & pagination (status, status_group, search, page/per_page);
- per-artifact integrity verification (a corrupt artifact does not mask others);
- job lifecycle action endpoints are wired and return sane statuses.
"""
import os

from fastapi.testclient import TestClient

from core.app import app


def _client():
    return TestClient(app, raise_server_exceptions=False)


# ── Structured timeline / events contract ────────────────────────

def test_job_has_structured_event_log_in_detail():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Event log contract"}).json()["job_id"]
    job = client.get(f"/api/jobs/{jid}").json()
    assert isinstance(job.get("event_log"), list)
    assert len(job["event_log"]) >= 1
    first = job["event_log"][0]
    for key in ("timestamp", "type", "message"):
        assert key in first, f"Missing structured event field: {key}"
    assert isinstance(job["events"], list)  # legacy string list still present


def test_job_events_endpoint_returns_structured_records():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Events endpoint contract"}).json()["job_id"]
    resp = client.get(f"/api/jobs/{jid}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert isinstance(events, list)
    assert len(events) >= 1
    for event in events:
        # Explicitly forbid string-lines and fabricated timestamps: every record
        # must carry a real backend timestamp plus structured fields.
        assert isinstance(event, dict)
        assert "timestamp" in event and event["timestamp"]
        assert "message" in event
        assert "type" in event


def test_job_events_endpoint_records_status_action():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Status event contract"}).json()["job_id"]
    client.post(f"/api/jobs/{jid}/cancel")
    events = client.get(f"/api/jobs/{jid}/events").json()
    status_events = [event for event in events if event.get("type") == "status"]
    assert status_events, "Expected at least one typed status event"
    assert any(event.get("state") == "CANCELLED" for event in status_events)


def test_job_events_endpoint_404_unknown_job():
    client = _client()
    resp = client.get("/api/jobs/doesnotexist-999/events")
    assert resp.status_code == 404


# ── Server-side job filters & pagination ─────────────────────────
def test_job_list_status_exact_filter():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Status filter contract"}).json()["job_id"]
    client.post(f"/api/jobs/{jid}/cancel")
    current = client.get(f"/api/jobs/{jid}").json()["status"]
    data = client.get("/api/jobs", params={"status": current}).json()
    assert isinstance(data, list)
    assert all(job["status"] == current for job in data)
    assert any(job["job_id"] == jid for job in data)


def test_job_list_status_group_filter_consistent_with_all():
    client = _client()
    client.post("/api/jobs", json={"topic": "Group filter contract"}).json()["job_id"]
    all_jobs = client.get("/api/jobs").json()
    grouped = client.get("/api/jobs", params={"status_group": "queued"}).json()
    assert isinstance(all_jobs, list)
    assert isinstance(grouped, list)
    all_ids = {job["job_id"] for job in all_jobs}
    grouped_ids = {job["job_id"] for job in grouped}
    assert grouped_ids <= all_ids  # grouped results are a subset


def test_job_list_search_filter():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "SearchableContract Topic"}).json()["job_id"]
    data = client.get("/api/jobs", params={"search": "searchablecontract"}).json()
    assert isinstance(data, list)
    assert any(job["job_id"] == jid for job in data)


def test_job_list_pagination_envelope():
    client = _client()
    for i in range(3):
        client.post("/api/jobs", json={"topic": f"Pagination contract {i}"})
    resp = client.get("/api/jobs", params={"page": 1, "per_page": 2})
    assert resp.status_code == 200
    data = resp.json()
    for key in ("items", "total", "page", "per_page", "pages", "has_more"):
        assert key in data, f"Missing pagination envelope field: {key}"
    assert data["page"] == 1
    assert len(data["items"]) <= 2
    assert data["total"] >= 3
    page2 = client.get("/api/jobs", params={"page": 2, "per_page": 2}).json()
    assert page2["page"] == 2
    assert page2["has_more"] is False


def test_job_list_legacy_array_when_no_page():
    client = _client()
    client.post("/api/jobs", json={"topic": "Legacy list contract"})
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Artifact integrity does not mask others ──────────────────────

def test_artifact_integrity_does_not_mask_others():
    from pathlib import Path

    from core.state import store as app_store
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Integrity contract"}).json()["job_id"]
    good = client.put(f"/api/jobs/{jid}/uploads/references/good.png",
                      content=b"\x89PNG\r\n\x1a\n\nvalid payload").json()
    bad = client.put(f"/api/jobs/{jid}/uploads/references/bad.png",
                     content=b"\x89PNG\r\n\x1a\n\ninitial payload").json()
    # Corrupt only the second artifact on disk, using the store's actual root so
    # the tamper targets the exact file the server verifies.
    bad_path = (app_store.root / jid / bad["path"]).resolve()
    bad_path.write_bytes(b"\x89PNG\r\n\x1a\n\ntampered content")
    resp = client.post(f"/api/jobs/{jid}/artifacts/verify")
    assert resp.status_code == 200
    data = resp.json()
    results = {item["artifact_id"]: item for item in data["results"]}
    # A corrupt artifact must NOT mask a healthy one.
    assert results[good["artifact_id"]]["valid"] is True
    assert results[bad["artifact_id"]]["valid"] is False
    assert data["valid"] is False


# ── Job lifecycle action endpoints are wired ─────────────────────

def test_job_lifecycle_actions_respond():
    client = _client()
    jid = client.post("/api/jobs", json={"topic": "Action contract"}).json()["job_id"]
    for action in ("pause", "resume", "retry", "regenerate", "cancel", "approve"):
        job = client.get(f"/api/jobs/{jid}").json()
        # Mutations only ever receive 200/400/409 — never a dropped route.
        resp = client.post(f"/api/jobs/{jid}/{action}")
        assert resp.status_code in (200, 400, 409), f"{action} -> {resp.status_code}"
    assert client.post("/api/jobs/doesnotexist-999/cancel").status_code == 404