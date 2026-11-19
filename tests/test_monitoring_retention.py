"""#503 AC2/AC3/AC5/AC7: Monitoring history in its own module schema, 3-day
default retention with automatic rotation, administrator-only extension.

Runs the module's signed migrations as the module role on a disposable
PostgreSQL (tests/module_postgres.py) through the platform ModuleDatabase.
"""

import json
import threading
from datetime import datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from core.module.data import ModuleDatabase
from core.module.lifecycle import bundled_manifest
from core.module.manifest import ModuleManifest
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "modules" / "monitoring"


@pytest.fixture
def monitoring(module_postgres, monkeypatch):
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    from monitoring_logic import MonitoringModule
    manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
    database = ModuleDatabase(module_postgres)
    credentials = database.ensure("monitoring")
    sources = {m["id"]: (MODULE_DIR / "migrations" / f"{m['id']}.sql").read_text() for m in manifest.migrations}
    assert database.migrate("monitoring", manifest.migrations, sources) == ["m001_initial", "m002_retention"]
    parsed = urlparse(module_postgres)
    credentials.update(host=parsed.hostname, port=parsed.port)
    module = MonitoringModule(credentials)
    module.database = database
    yield module
    database.drop("monitoring")


def _metric(module, name, at):
    from monitoring_logic import Metric
    module.record_metric(Metric(id=f"{name}-{at.timestamp()}", name=name, value=1.0, labels={},
                                timestamp=at.isoformat(), node_id="n"))


def test_three_day_default_rotation_keeps_newer_history_and_unresolved_alerts(monitoring):
    from monitoring_logic import Alert
    now = datetime(2026, 11, 19, 12, 0, 0)
    assert monitoring.retention_days() == 3
    _metric(monitoring, "old", now - timedelta(days=3, seconds=1))
    _metric(monitoring, "edge", now - timedelta(days=3) + timedelta(seconds=1))
    _metric(monitoring, "new", now - timedelta(hours=1))
    monitoring.create_alert(Alert(id="open", name="a", severity="warning", condition="c", message="", active=True,
                                  fired_at=(now - timedelta(days=10)).isoformat(), resolved_at=None, labels={}))
    monitoring.create_alert(Alert(id="closed", name="b", severity="info", condition="c", message="", active=False,
                                  fired_at=(now - timedelta(days=10)).isoformat(),
                                  resolved_at=(now - timedelta(days=5)).isoformat(), labels={}))
    removed = monitoring.rotate(now)
    assert removed["metrics"] == 1 and removed["alerts"] == 1
    assert sorted(m.name for m in monitoring.get_metrics()) == ["edge", "new"]
    assert [a.id for a in monitoring.list_alerts()] == ["open"]


def test_administrator_extends_retention_and_it_survives_a_restart(monitoring):
    from monitoring_logic import MonitoringModule
    now = datetime(2026, 11, 19, 12, 0, 0)
    _metric(monitoring, "five-days", now - timedelta(days=5))
    assert monitoring.set_retention_days(7, "admin") == 7
    restarted = MonitoringModule(monitoring.credentials)  # a new container process
    assert restarted.retention_days() == 7
    assert restarted.rotate(now)["metrics"] == 0
    assert [m.name for m in restarted.get_metrics()] == ["five-days"]  # history read back after restart
    for invalid in (2, 0, -1, "7", 7.5, True, 100000):
        with pytest.raises(ValueError):
            restarted.set_retention_days(invalid)
    assert restarted.retention_days() == 7


def test_history_and_settings_are_in_the_module_schema_export(monitoring):
    """The uninstall archive is built from this export; rotation never touches it."""
    _metric(monitoring, "kept", datetime.utcnow())
    monitoring.set_retention_days(5)
    exported = monitoring.database.export("monitoring")
    assert exported["schema"] == "mod_monitoring"
    assert "kept" in exported["tables"]["metrics"]
    assert "retention_days,5" in exported["tables"]["settings"]


def test_settings_change_needs_the_administrator_through_the_gateway(monitoring, monkeypatch):
    import monitoring_module
    monkeypatch.setattr(monitoring_module, "TOKEN", "module-token")
    monkeypatch.setattr(monitoring_module, "_module", {"value": monitoring})
    server = ThreadingHTTPServer(("127.0.0.1", 0), monitoring_module.MonitoringHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def call(method, path, role=None, body=None, token="module-token"):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            headers = {"content-type": "application/json", "x-vertep-module-token": token}
            if role:
                headers["x-vertep-role"] = role
            connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            status, data = response.status, json.loads(response.read() or b"null")
            connection.close()
            return status, data

        assert call("GET", "/health", token="")[0] == 200  # liveness only
        assert call("GET", "/settings/retention", token="forged")[0] == 403
        assert call("GET", "/settings/retention", role="moderator") == (200, {"retention_days": 3})
        assert call("PUT", "/settings/retention", role="moderator", body={"retention_days": 10})[0] == 403
        assert call("POST", "/rotate", role="moderator")[0] == 403
        assert call("PUT", "/settings/retention", role="admin", body={"retention_days": 1})[0] == 422
        assert call("PUT", "/settings/retention", role="admin", body={"retention_days": 10}) == (200, {"retention_days": 10})
        status, data = call("POST", "/rotate", role="admin")
        assert status == 200 and set(data["removed"]) == {"metrics", "health_checks", "system_logs", "alerts"}
    finally:
        server.shutdown()
        server.server_close()


def test_manifest_declares_retention_routes_migrations_and_no_core_only_placement():
    manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
    assert {"/settings/retention", "/rotate", "/health-checks"} <= set(manifest.api["routes"])
    assert [m["id"] for m in manifest.migrations] == ["m001_initial", "m002_retention"]
    assert manifest.placement["target"] == "any" and manifest.placement["requires_gpu"] is False
    for migration in ("m001_initial", "m002_retention"):
        assert "monitoring." not in (MODULE_DIR / "migrations" / f"{migration}.sql").read_text()  # own schema only
