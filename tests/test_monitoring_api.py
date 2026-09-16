"""i.0.0.0.52 (#19) — runtime contracts for CORE monitoring APIs.

Covers the defects raised in Issue #53's Monitoring block: the async
``/api/watchdog/report`` handler (valid/invalid body + persistence),
``/api/health`` error semantics and ``/api/alerts`` lifecycle behaviour.
"""
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from core.app import app


def _client(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return TestClient(app, raise_server_exceptions=False), Path(str(tmp_path))


def _worker_headers(node_name: str = "test-node") -> dict:
    return {"x-vertep-node-name": node_name}


# ── /api/watchdog/report ────────────────────────────────────────────────

def test_watchdog_accepts_valid_body_and_persists(monkeypatch, tmp_path):
    client, update_dir = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    payload = {"role": "gpu", "restarted": True, "checks": {"gpu": ["True", "Tesla T4"]}}
    resp = client.post("/api/watchdog/report", json=payload, headers=_worker_headers())
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True}
    lines = (update_dir / "watchdog-reports.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["payload"] == payload
    assert record["received_at"]


def test_watchdog_rejects_invalid_worker_token(monkeypatch, tmp_path):
    client, update_dir = _client(monkeypatch, tmp_path, NODE_API_TOKEN="some-expected-token")
    resp = client.post("/api/watchdog/report", json={"role": "text"},
                       headers={"x-vertep-node-name": "text-1",
                                "x-vertep-token": "wrong-token"})
    assert resp.status_code == 401
    assert not (update_dir / "watchdog-reports.jsonl").exists()


def test_watchdog_accepts_invalid_body_as_empty_payload(monkeypatch, tmp_path):
    client, update_dir = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    resp = client.post("/api/watchdog/report",
                       content="{not valid json", headers=_worker_headers())
    assert resp.status_code == 200
    record = json.loads((update_dir / "watchdog-reports.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["payload"] == {}


def test_watchdog_reports_accumulate_history(monkeypatch, tmp_path):
    client, update_dir = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    for i in range(3):
        resp = client.post("/api/watchdog/report", json={"seq": i}, headers=_worker_headers())
        assert resp.status_code == 200
    history = [json.loads(line) for line in
               (update_dir / "watchdog-reports.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["payload"]["seq"] for item in history] == [0, 1, 2]


# ── /api/health/ready (readiness gate) ──────────────────────────────────

def _patch_checks(monkeypatch, checks):
    # Falsy lists/tuples are not required for testenv; force a fully controlled result.
    status = "UNHEALTHY" if any(isinstance(v, tuple) and v[0] is False for v in checks.values()) else "HEALTHY"
    monkeypatch.setattr("core.api.observability._run_health_checks", lambda _role=None: checks)
    monkeypatch.setattr("core.api.observability._health_status", lambda c: status)
    return status


def test_readiness_healthy_returns_200(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    _patch_checks(monkeypatch, {"docker": (True, "ok")})
    resp = client.get("/api/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "HEALTHY"


def test_readiness_unhealthy_returns_503(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    _patch_checks(monkeypatch, {"docker": (False, "docker daemon down")})
    resp = client.get("/api/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "UNHEALTHY"


def test_not_applicable_checks_do_not_fail_readiness(monkeypatch, tmp_path):
    from core.health_checks import health_status
    checks = {"postgres": (None, "not-applicable: DATABASE_URL is not configured"),
              "redis": (None, "not-applicable: REDIS_URL is not configured"),
              "docker": (True, "ok")}
    assert health_status(checks) == "HEALTHY"


def test_required_but_unavailable_fails_readiness(monkeypatch, tmp_path):
    from core.health_checks import health_status
    checks = {"docker": (False, "docker daemon down")}
    assert health_status(checks) == "UNHEALTHY"


def test_check_postgres_redis_mark_not_applicable_when_unconfigured(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    from core.health_checks import check_postgres, check_redis
    assert check_postgres()[0] is None
    assert check_redis()[0] is None


# ── Persistent alert store ─────────────────────────────────────────────

def test_alert_dedup_by_entity_key(tmp_path):
    from core.alert_store import AlertStore
    store = AlertStore(path=tmp_path / "alerts.json")
    first = store.record({"type": "WORKER_OFFLINE", "node_name": "gpu-1", "severity": "error"})
    second = store.record({"type": "WORKER_OFFLINE", "node_name": "gpu-1", "severity": "error"})
    assert first["id"] == second["id"]
    assert len(store.list()) == 1


def test_alert_lifecycle_acknowledge_and_resolve(tmp_path):
    from core.alert_store import AlertStore
    store = AlertStore(path=tmp_path / "alerts.json")
    alert = store.record({"type": "SYSTEM_STATE", "message": "down"})
    assert alert["state"] == "firing"
    acked = store.acknowledge(alert["id"], "admin")
    assert acked["state"] == "acknowledged"
    assert acked["acknowledged_by"] == "admin"
    resolved = store.resolve({"type": "SYSTEM_STATE"}, "recovered")
    assert resolved[0]["state"] == "resolved"
    # Resolved history is retained, not erased.
    assert len(store.list()) == 1
    assert store.list()[0]["state"] == "resolved"
    # Resolve is idempotent.
    assert store.resolve({"type": "SYSTEM_STATE"}, "again") == []


def test_alert_cannot_acknowledge_resolved(tmp_path):
    from core.alert_store import AlertStore
    store = AlertStore(path=tmp_path / "alerts.json")
    alert = store.record({"type": "JOB_FAILED", "job_id": "j-1"})
    store.resolve({"type": "JOB_FAILED", "job_id": "j-1"}, "recovered")
    try:
        store.acknowledge(alert["id"], "admin")
        raise AssertionError("expected ValueError for resolved alert")
    except ValueError as error:
        assert "resolved" in str(error)


def test_alert_persisted_and_reloaded(tmp_path):
    from core.alert_store import AlertStore
    path = tmp_path / "alerts.json"
    store = AlertStore(path=path)
    store.record({"type": "UPDATE_FAILED", "message": "boom", "details": ["x"]})
    reloaded = AlertStore(path=path)
    assert len(reloaded.list()) == 1
    assert reloaded.list()[0]["type"] == "UPDATE_FAILED"
    assert reloaded.list()[0]["details"] == ["x"]


def test_alert_retention_prunes_old_resolved(tmp_path):
    from core.alert_store import AlertStore
    store = AlertStore(path=tmp_path / "alerts.json", retention_days=0, max_active=100)
    alert = store.record({"type": "JOB_FAILED", "job_id": "j-old"})
    store.resolve({"type": "JOB_FAILED", "job_id": "j-old"}, "done")
    # Age the resolved record far beyond the retention window, then force a prune.
    alert["resolved_at"] = "2020-01-01T00:00:00Z"
    alert["updated_at"] = "2020-01-01T00:00:00Z"
    store.save()
    store.record({"type": "SYSTEM_STATE"})
    assert all(item.get("type") != "JOB_FAILED" for item in store.list())


def test_acknowledge_alert_endpoint(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    from core.alert_store import get_alert_store
    alert = get_alert_store().record({"type": "SYSTEM_STATE", "message": "drift"})
    resp = client.post(f"/api/alerts/{alert['id']}/acknowledge", json={"actor": "operator"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "acknowledged"
    assert resp.json()["acknowledged_by"] == "operator"
    assert client.post("/api/alerts/does-not-exist/acknowledge", json={"actor": "operator"}).status_code == 404


# ── Log retrieval (rotated files + filter-before-tail + paging) ─────────

def _write_logs(tmp_path, base_lines, rotated_lines=None):
    root = tmp_path / "logs"
    root.mkdir(exist_ok=True)
    (root / "app.jsonl").write_text("\n".join(base_lines) + "\n", encoding="utf-8")
    if rotated_lines:
        (root / "app.jsonl.1").write_text("\n".join(rotated_lines) + "\n", encoding="utf-8")
    return root


def test_read_logs_includes_rotated_backups(monkeypatch, tmp_path):
    base = ['{"timestamp":"2026-01-01T00:00:03Z","level":"INFO","job_id":"j2","node_name":"n1","message":"b3"}',
            '{"timestamp":"2026-01-01T00:00:02Z","level":"ERROR","job_id":"j1","node_name":"n2","message":"b2"}']
    rotated = ['{"timestamp":"2026-01-01T00:00:01Z","level":"INFO","job_id":"j1","node_name":"n1","message":"a1"}',
               '{"timestamp":"2026-01-01T00:00:00Z","level":"INFO","job_id":"jx","node_name":"n9","message":"a0"}']
    root = _write_logs(tmp_path, base, rotated)
    monkeypatch.setenv("LOG_ROOT", str(root))
    from core.logging_config import read_logs
    job1 = read_logs(job_id="j1")
    assert {item["job_id"] for item in job1} == {"j1"}  # records with job_id only
    assert sorted(item["message"] for item in job1) == ["a1", "b2"]
    assert len(read_logs()) == 4


def test_read_logs_filters_before_tail_across_rotations(monkeypatch, tmp_path):
    base = ['{"timestamp":"2026-01-01T00:00:%02dZ","level":"INFO","job_id":"j1","node_name":"n1","message":"x"}' % i
            for i in range(5, 10)]
    rotated = ['{"timestamp":"2026-01-01T00:00:01Z","level":"ERROR","job_id":"j1","node_name":"n1","message":"err-rotated"}']
    root = _write_logs(tmp_path, base, rotated)
    monkeypatch.setenv("LOG_ROOT", str(root))
    from core.logging_config import read_logs
    errors = read_logs(limit=1, level="ERROR")
    assert len(errors) == 1
    assert errors[0]["message"] == "err-rotated"
    assert errors[0]["job_id"] == "j1"


def test_read_logs_pages_with_before_cursor(monkeypatch, tmp_path):
    lines = ['{"timestamp":"2026-01-01T00:00:%02dZ","level":"INFO","job_id":"j1","node_name":"n1","message":"m%02d"}' % (i, i)
             for i in range(0, 5)]
    root = _write_logs(tmp_path, lines)
    monkeypatch.setenv("LOG_ROOT", str(root))
    from core.logging_config import read_logs
    page = read_logs(limit=1, before="2026-01-01T00:00:03Z")
    assert len(page) == 1
    assert page[0]["message"] == "m02"  # strictly older than cursor, newest first
    all_entries = read_logs()
    assert [item["message"] for item in all_entries] == [f"m{i:02d}" for i in range(4, -1, -1)]


# ── Log redaction (Issue #53, #18 block) ───────────────────────────────

def test_secret_redact_masks_key_value_and_bearer():
    from core.logging_config import secret_redact
    out = secret_redact("auth failed access_token=synthetic-audit-secret and token:abc123 plus Bearer xyz.1-23")
    assert "synthetic-audit-secret" not in out
    assert "abc123" not in out
    assert "xyz.1-23" not in out
    assert "[REDACTED]" in out


def test_json_formatter_does_not_leak_synthetic_secret():
    import logging
    from core.logging_config import JsonFormatter
    record = logging.LogRecord("core", logging.ERROR, "f", 1,
                               "failed ingestion access_token=synthetic-audit-secret", None, None)
    output = JsonFormatter().format(record)
    assert "synthetic-audit-secret" not in output
    assert "[REDACTED]" in output


def test_ingest_logs_redacts_worker_text(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, NODE_API_TOKEN=None)
    recorded = []
    fake_logger = type("FakeLogger", (), {"log": staticmethod(lambda level, msg, extra=None: recorded.append(msg))})()
    monkeypatch.setattr("core.api.observability.logger", fake_logger)
    resp = client.post("/api/logs/ingest", json={
        "node_name": "text-1",
        "entries": [{"level": "ERROR", "message": "worker blew up refresh_token=synthetic-audit-secret",
                     "job_id": "j-9"}],
    }, headers={"x-vertep-node-name": "text-1"})
    assert resp.status_code == 200
    assert len(recorded) == 1
    assert "synthetic-audit-secret" not in recorded[0]
    assert "[REDACTED]" in recorded[0]