"""#442 D13: health of a CORE host with roles is not HEALTHY while its Worker is offline."""
import json
from datetime import datetime, timezone

from core import health_checks


def test_core_roles_need_the_live_local_worker(monkeypatch, tmp_path):
    (tmp_path / "deployment-plan.json").write_text(json.dumps({"role": "core", "additional_roles": ["text", "backup"]}))
    (tmp_path / "installation.json").write_text(json.dumps({"completed_at": "2026-11-24T00:00:00Z", "node_role": "core"}))
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("NODE_NAME", "vertep")
    workers = []
    monkeypatch.setattr("core.state.store.load_workers", lambda: workers)
    checks = health_checks.run_checks("core")
    assert "local_worker" in checks["required"] and checks["local_worker"] == (False, "health_worker_not_ready")
    assert health_checks.health_status(checks) == "UNHEALTHY"
    workers.append({"node_name": "vertep", "status": "FREE", "last_seen": datetime.now(timezone.utc).isoformat()})
    assert health_checks.run_checks("core")["local_worker"] == (True, "health_worker_ready")
    (tmp_path / "deployment-plan.json").write_text(json.dumps({"role": "core", "additional_roles": []}))
    assert "local_worker" not in health_checks.run_checks("core")  # CORE alone runs no roles
