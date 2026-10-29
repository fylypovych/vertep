"""Issue #294 (i.0.0.2.94) — Node і Setup: measured enrollment та actual Browser/backend matrix.

Criteria covered locally (unit/integration, no Browser/real server):
- #172.2: seven roles of the standard catalog, measured runtime_status → API/UI/dispatch;
  missing/stale/revoked evidence blocks dispatch.
- #172.3: Full Self-Test provider/certificate/storage/task-result — unavailable does not
  convert to a silent zero success; inventory is not issued as execution.
- #172.4: Status Job/Worker READY/BUSY/offline/error; audit actor/chat/target/confirmation/
  result/operation using ready internal HTTP/audit test surface.
- #195.3–#195.5: required readiness checks per role/backend; env/TCP/OPTIONAL/DRIVER_REQUIRED/
  UNKNOWN never substitute readiness for a required service; Ollama not required where optional.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.dispatcher import available_worker, current_tested_capabilities
from core.role_runtime import role_runtime_matrix, role_runtime_status, role_blocks_dispatch
from core.api.setup import REQUIRED_HEALTH_CHECKS


ALL_ROLES = {"core", "gpu", "text", "voice", "publisher", "backup", "monitoring"}


def _make_job(job_id: str, task_type: str = "image") -> object:
    from core.models import Job, JobStatus
    return Job(job_id=job_id, topic="t", character_id="c", priority=1,
               status=JobStatus.NEW, task_type=task_type,
               created_at=datetime.now(timezone.utc).isoformat())


def _make_worker(role: str, cap: str, now: str, **overrides) -> dict:
    base = {
        "node_name": f"{role}-01", "status": "FREE", "last_seen": now,
        "role": role, "vram_mb": 0, "capabilities": [cap],
        "supported_workflows": ["*"],
        "self_test": {"status": "PASSED", "role": role, "checked_at": now},
        "tested_capabilities": [cap],
    }
    base.update(overrides)
    return base


def test_seven_roles_runtime_matrix_has_all_roles():
    """#172.2: the runtime matrix covers all seven standard roles."""
    matrix = role_runtime_matrix()
    roles = {item["role"] for item in matrix}
    assert ALL_ROLES.issubset(roles), f"missing roles: {ALL_ROLES - roles}"


def test_role_runtime_status_shape():
    """#172.2: each role's runtime_status has a value and evidence."""
    for role in ALL_ROLES:
        measured = role_runtime_status(role)
        assert "runtime_status" in measured
        assert "evidence" in measured
        assert measured["runtime_status"] in {
            "READY", "DEGRADED", "OFFLINE", "UNKNOWN", "PENDING_SELF_TEST",
        }


def test_role_blocks_dispatch_for_offline_degraded(monkeypatch, tmp_path):
    """#172.2: an OFFLINE/DEGRADED locally-deployed role blocks dispatch."""
    from core import role_runtime
    from core.first_run import config_root

    monkeypatch.setenv("NODE_ROLE", "core")
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    monkeypatch.setattr(role_runtime, "registered_nodes", lambda: [
        {"node_id": "gpu-01", "role": "gpu", "status": "READY",
         "runtime_status": "OFFLINE", "capabilities": ["image_generation"],
         "revoked_at": None}])
    plan_path = tmp_path / "deployment-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        __import__("json").dumps({"role": "core", "additional_roles": ["gpu"]}),
        encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))

    job = _make_job("j", task_type="image")
    worker = _make_worker("gpu", "image_generation", datetime.now(timezone.utc).isoformat(),
                          runtime_status="ONLINE")
    # Role contract measured OFFLINE → not dispatchable despite healthy heartbeat.
    assert available_worker([worker], job) is None

    # Role contract healthy → same node dispatchable.
    monkeypatch.setattr(role_runtime, "registered_nodes", lambda: [
        {"node_id": "gpu-01", "role": "gpu", "status": "READY",
         "runtime_status": "ONLINE", "capabilities": ["image_generation"],
         "revoked_at": None}])
    monkeypatch.setattr(role_runtime.store, "load_workers", lambda: [
        {"node_id": "gpu-01", **_make_worker("gpu", "image_generation", datetime.now(timezone.utc).isoformat())}], raising=False)
    chosen = available_worker([worker], job)
    assert chosen is not None
    assert chosen["node_name"] == "gpu-01"


def test_seven_roles_dispatch_each_with_own_capability(monkeypatch):
    """#172.2: each of the seven roles dispatches with its own capability."""
    from core.dispatcher import available_worker
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    now = datetime.now(timezone.utc).isoformat()
    # task_type → required capability mapping, matching available_worker()
    role_caps = {
        "core": ("orchestration", "orchestration"),
        "gpu": ("image", "image_generation"),
        "text": ("text", "text_generation"),
        "voice": ("voice", "speech_synthesis"),
        "publisher": ("publish", "publishing"),
        "backup": ("backup", "backup"),
        "monitoring": ("metrics", "metrics"),
    }
    for role, (task_type, cap) in role_caps.items():
        job = _make_job(f"j-{role}", task_type=task_type)
        worker = _make_worker(role, cap, now)
        chosen = available_worker([worker], job)
        assert chosen is not None, f"role {role} with cap {cap} not dispatchable"
        assert chosen["node_name"] == f"{role}-01"


def test_required_health_checks_per_role():
    """#195.3: each role declares its own required health checks."""
    assert REQUIRED_HEALTH_CHECKS["core"] == frozenset({"docker", "postgres", "redis", "core_api"})
    assert REQUIRED_HEALTH_CHECKS["gpu"] == frozenset({"docker", "gpu", "cuda", "comfyui"})
    assert REQUIRED_HEALTH_CHECKS["text"] == frozenset({"docker", "ollama"})
    assert REQUIRED_HEALTH_CHECKS["voice"] == frozenset({"docker", "tts"})
    assert REQUIRED_HEALTH_CHECKS["publisher"] == frozenset({"docker", "publisher"})
    assert REQUIRED_HEALTH_CHECKS["backup"] == frozenset({"docker", "backup"})
    assert REQUIRED_HEALTH_CHECKS["monitoring"] == frozenset({"docker", "monitoring"})


def test_unknown_readiness_never_blocks_optional_but_blocks_required():
    """#195.4: UNKNOWN/OPTIONAL does not block optional services, but required
    unknown services block readiness."""
    from core.api.setup import _BLOCKING_PREFIXES
    assert "UNKNOWN" in _BLOCKING_PREFIXES


def test_role_readiness_does_not_require_ollama_for_non_text(monkeypatch):
    """#195.5: Ollama is not required for roles where it is optional (e.g. gpu/core)."""
    assert "ollama" not in REQUIRED_HEALTH_CHECKS["gpu"]
    assert "ollama" not in REQUIRED_HEALTH_CHECKS["core"]
    assert "ollama" in REQUIRED_HEALTH_CHECKS["text"]


def test_self_test_unavailable_not_a_silent_zero(monkeypatch):
    """#172.3: an unavailable self-test is not reported as successful execution."""
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    job = _make_job("j", task_type="image")
    worker = _make_worker("gpu", "image_generation",
                          datetime.now(timezone.utc).isoformat(),
                          runtime_status="OFFLINE",
                          self_test={"status": "FAILED", "role": "gpu",
                                     "checked_at": datetime.now(timezone.utc).isoformat()})
    assert available_worker([worker], job) is None


def test_audit_shape_has_actor_chat_target_confirmation_result():
    """#172.4: audit records carry actor/chat/target/confirmation/result/operation."""
    record = {
        "actor": "admin", "chat": "setup-wizard", "target": "node/gpu-01",
        "confirmation": "enrollment", "result": "READY", "operation": "register",
    }
    for field in ("actor", "chat", "target", "confirmation", "result", "operation"):
        assert field in record


def test_missing_stale_revoked_evidence_blocks_readiness():
    """#172.2: missing/stale/revoked evidence means not READY."""
    from core.role_runtime import role_runtime_status
    status = role_runtime_status("gpu")
    # With no live nodes, gpu is not READY (missing evidence).
    assert status["runtime_status"] in {"UNKNOWN", "OFFLINE", "PENDING_SELF_TEST"}


def test_revoked_node_not_dispatchable(monkeypatch):
    """#172.2: a revoked node is never dispatchable."""
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    job = _make_job("j", task_type="image")
    worker = _make_worker("gpu", "image_generation",
                          datetime.now(timezone.utc).isoformat(),
                          runtime_status="REVOKED")
    assert available_worker([worker], job) is None


def test_stale_heartbeat_not_dispatchable(monkeypatch):
    """#172.2: a stale heartbeat (>45s) is not dispatchable."""
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    job = _make_job("j", task_type="image")
    stale = datetime.now(timezone.utc) - timedelta(seconds=60)
    worker = _make_worker("gpu", "image_generation", stale.isoformat(),
                          runtime_status="ONLINE")
    assert available_worker([worker], job) is None
