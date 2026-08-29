from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import app as core_app
from core.health_gate import FINAL, POST_UPDATE, evaluate_worker, gate_report

ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
VERSION = "1.5.0"


def core_snapshot(**overrides) -> dict:
    snapshot = {
        "postgres": {"reachable": True, "applied_migrations": ["001_initial.sql"],
                     "expected_migrations": ["001_initial.sql"], "error": ""},
        "redis": {"backend": "redis", "roundtrip": True, "error": ""},
        "api": {"status": "ok", "version": VERSION},
        "web_ui": {"status_code": 200, "error": ""},
        "ollama": {"required": True, "reachable": True, "error": ""},
        "dispatcher": {"registered_workers": 1, "eligible_workers": 1},
        "nodes": [{"node_id": "gpu-01", "revoked_at": None}],
        "workers": [{"node_name": "gpu-01", "status": "READY", "version": VERSION}],
        "system_state": "NORMAL",
        "queue": {"paused": False, "inflight": 0, "depth": 0, "waiting_for_system": 0},
    }
    snapshot.update(overrides)
    return snapshot


def worker_snapshot(**overrides) -> dict:
    snapshot = {
        "gpu_required": True, "gpu_available": True, "gpu_name": "Tesla P100",
        "driver_version": "550.90", "cuda_version": "12.6",
        "free_vram_mb": 16000, "vram_mb": 16000, "min_vram_mb": 8000,
        "comfyui": {"required": True, "reachable": True, "error": ""},
        "worker_api": {"reachable": True},
        "self_test": {"status": "PASSED", "role": "gpu", "checked_at": (NOW - timedelta(seconds=60)).isoformat()},
        "version": VERSION,
    }
    snapshot.update(overrides)
    return snapshot


def failed(report: dict) -> list[str]:
    return report["failed"]


def test_healthy_core_passes_the_gate():
    report = gate_report("core", core_snapshot(), VERSION, FINAL, now=NOW)
    assert report["passed"], failed(report)
    assert {check["name"] for check in report["checks"]} >= {
        "postgres", "redis", "api", "web_ui", "ollama", "dispatcher", "workers_ready", "queue_resumed"}


@pytest.mark.parametrize("override,expected", [
    ({"postgres": {"reachable": False, "error": "connection refused"}}, "postgres"),
    ({"postgres": {"reachable": True, "applied_migrations": [],
                   "expected_migrations": ["001_initial.sql"]}}, "postgres"),
    ({"redis": {"backend": "redis", "roundtrip": False, "error": "timeout"}}, "redis"),
    ({"api": {"status": "ok", "version": "1.4.0"}}, "api"),
    ({"web_ui": {"status_code": 502}}, "web_ui"),
    ({"ollama": {"required": True, "reachable": False, "error": "refused"}}, "ollama"),
    ({"dispatcher": {"registered_workers": 2, "eligible_workers": 0}}, "dispatcher"),
    ({"workers": [{"node_name": "gpu-01", "status": "ERROR", "version": VERSION}]}, "workers_ready"),
    ({"queue": {"paused": True, "inflight": 0, "waiting_for_system": 0}}, "queue_resumed"),
    ({"queue": {"paused": False, "inflight": 2, "waiting_for_system": 0}}, "queue_drained"),
    ({"queue": {"paused": False, "inflight": 0, "waiting_for_system": 3}}, "jobs_released"),
    ({"system_state": "MAINTENANCE"}, "system_state"),
])
def test_any_broken_dependency_fails_the_gate(override, expected):
    report = gate_report("core", core_snapshot(**override), VERSION, FINAL, now=NOW)
    assert not report["passed"]
    assert expected in failed(report)


def test_post_update_mode_ignores_queue_resumption_but_requires_drain():
    paused = core_snapshot(queue={"paused": True, "inflight": 0}, system_state="UPDATING")
    assert gate_report("core", paused, VERSION, POST_UPDATE, now=NOW)["passed"]
    inflight = core_snapshot(queue={"paused": True, "inflight": 1}, system_state="UPDATING")
    assert "queue_drained" in failed(gate_report("core", inflight, VERSION, POST_UPDATE, now=NOW))


def test_missing_node_heartbeat_is_a_failure_unless_explicitly_allowed(monkeypatch):
    snapshot = core_snapshot(workers=[])
    assert "workers_ready" in failed(gate_report("core", snapshot, VERSION, FINAL, now=NOW))
    monkeypatch.setenv("HEALTH_GATE_ALLOW_OFFLINE_NODES", "true")
    assert gate_report("core", snapshot, VERSION, FINAL, now=NOW)["passed"]


def test_revoked_nodes_do_not_block_the_gate():
    snapshot = core_snapshot(nodes=[{"node_id": "gpu-01", "revoked_at": None},
                                    {"node_id": "gpu-02", "revoked_at": "2026-08-01T00:00:00+00:00"}])
    assert gate_report("core", snapshot, VERSION, FINAL, now=NOW)["passed"]


def test_fleet_version_drift_is_advisory_until_required(monkeypatch):
    snapshot = core_snapshot(workers=[{"node_name": "gpu-01", "status": "READY", "version": "1.4.0"}])
    assert gate_report("core", snapshot, VERSION, FINAL, now=NOW)["passed"]
    monkeypatch.setenv("HEALTH_GATE_REQUIRE_FLEET_VERSION", "true")
    assert "fleet_version" in failed(gate_report("core", snapshot, VERSION, FINAL, now=NOW))


def test_healthy_worker_passes_the_gate():
    report = gate_report("worker", worker_snapshot(), VERSION, POST_UPDATE, now=NOW)
    assert report["passed"], failed(report)


@pytest.mark.parametrize("override,expected", [
    ({"gpu_available": False}, "gpu"),
    ({"driver_version": "unknown"}, "driver"),
    ({"cuda_version": ""}, "cuda"),
    ({"free_vram_mb": 512}, "vram"),
    ({"comfyui": {"required": True, "reachable": False, "error": "refused"}}, "comfyui"),
    ({"worker_api": {"reachable": False, "error": "not serving"}}, "worker_api"),
    ({"self_test": {"status": "FAILED", "checked_at": NOW.isoformat()}}, "self_test"),
    ({"version": "1.4.0"}, "worker_version"),
])
def test_broken_worker_runtime_fails_the_gate(override, expected):
    report = gate_report("worker", worker_snapshot(**override), VERSION, POST_UPDATE, now=NOW)
    assert not report["passed"]
    assert expected in failed(report)


def test_stale_self_test_is_not_accepted():
    stale = worker_snapshot(self_test={"status": "PASSED", "role": "gpu",
                                       "checked_at": (NOW - timedelta(hours=3)).isoformat()})
    assert "self_test" in failed(gate_report("worker", stale, VERSION, POST_UPDATE, now=NOW))


def test_worker_without_gpu_requirement_ignores_accelerator_checks():
    snapshot = worker_snapshot(gpu_required=False, gpu_available=False, driver_version="unknown",
                               cuda_version="unknown", free_vram_mb=0,
                               comfyui={"required": False, "reachable": False, "error": ""})
    assert gate_report("worker", snapshot, VERSION, POST_UPDATE, now=NOW)["passed"]


def test_core_worker_role_evaluates_both_sides():
    snapshot = {**core_snapshot(), "worker": worker_snapshot(gpu_available=False)}
    report = gate_report("core-worker", snapshot, VERSION, FINAL, now=NOW)
    assert not report["passed"]
    assert "gpu" in failed(report)
    assert {check["component"] for check in report["checks"]} == {"core", "fleet", "worker"}


def test_unknown_role_and_mode_are_rejected():
    with pytest.raises(ValueError):
        gate_report("monitoring", core_snapshot(), VERSION, FINAL, now=NOW)
    with pytest.raises(ValueError):
        gate_report("core", core_snapshot(), VERSION, "whatever", now=NOW)


def test_worker_evaluation_reports_every_specified_check():
    names = {check.name for check in evaluate_worker(worker_snapshot(), VERSION, now=NOW)}
    assert names == {"gpu", "driver", "cuda", "vram", "comfyui", "worker_api", "self_test", "worker_version"}


def test_update_pipeline_gates_on_the_full_health_report():
    cli = (ROOT / "scripts" / "vertep").read_text(encoding="utf-8")
    apply_update = cli.split("apply-update)", 1)[1].split("health-gate)", 1)[0]
    assert "scripts/health-gate.py" in apply_update
    assert "--mode post-update" in apply_update
    assert 'curl -fsS -u "${ADMIN_USER:-admin}' not in apply_update
    agent = (ROOT / "scripts" / "update-agent.py").read_text(encoding="utf-8")
    assert "final_gate(root, state_dir, state, manifest[\"version\"])" in agent
    assert '"--mode", "final"' in agent


def test_api_exposes_the_full_health_report(monkeypatch):
    monkeypatch.setattr(core_app, "probe_postgres", lambda: {"reachable": True, "applied_migrations": [],
                                                             "expected_migrations": [], "error": ""})
    monkeypatch.setattr(core_app, "probe_redis", lambda: {"backend": "redis", "roundtrip": True, "error": ""})
    monkeypatch.setattr(core_app, "probe_ollama", lambda: {"required": False, "reachable": False, "error": ""})
    report = TestClient(core_app.app).get("/api/system/health/full?mode=final").json()
    assert report["mode"] == "final"
    assert report["version"] == core_app.application_version()
    assert {check["name"] for check in report["checks"]} >= {"postgres", "redis", "api", "web_ui",
                                                             "dispatcher", "queue_resumed"}
    assert report["passed"], report["failed"]
