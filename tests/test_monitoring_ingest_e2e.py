"""#503 AC1/AC4/AC5/AC6: the Monitoring module end to end on a disposable PostgreSQL.

The bundled package is installed through the platform lifecycle and placed by
the administrator on a compatible non-CORE node (an incompatible one is refused
with the reason). The module ingests CORE's existing collectors through its
declared ``monitoring.read`` API: metrics, health checks and alert states from
the persistent alert store, including the fire -> acknowledge -> recovery
history. No thresholds or rules are evaluated by the module. Uninstall writes a
verified archive that restores the history; CORE keeps serving health, metrics
and alerts without the module.
"""

import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from core.module.data import ModuleDatabase
from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import ModuleLifecycle, bundled_manifest
from core.module.manifest import ModuleManifest
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)
from tests.test_characters_module import api  # noqa: F401  (fixture)

MODULE_DIR = Path(__file__).resolve().parents[1] / "modules" / "monitoring"


@pytest.fixture
def monitoring(api, tmp_path, monkeypatch, module_postgres):
    from core.module.gateway import issue_token
    from core.state import store
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    import monitoring_module
    from monitoring_logic import MonitoringModule
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.4.12"}, PlacementStore(tmp_path / "placement.json"))
    credentials = {}
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda package: None, database=ModuleDatabase(module_postgres),
                                registry=registry, system_versions={"core_runtime": "0.0.4.12"},
                                store_credentials=lambda module_id, value: credentials.__setitem__(module_id, value),
                                drain_timeout=1.0)
    assert lifecycle.install_bundled("monitoring")["state"] == "INSTALLED"
    registry.approve_permissions("monitoring", ["monitoring.read"])
    assert lifecycle.activate("monitoring")["state"] == "ACTIVE"
    parsed = urlparse(module_postgres)
    database = {**credentials["monitoring"], "host": parsed.hostname, "port": parsed.port}
    monkeypatch.setitem(monitoring_module._module, "value", MonitoringModule(database))
    monkeypatch.setattr(monitoring_module, "TOKEN", issue_token("monitoring", "text-node"))

    def core_observability():  # the HTTP hop to CORE, through the real endpoint
        response = api.get("/api/modules/self/observability",
                           headers={"x-vertep-module-token": monitoring_module.TOKEN, "Authorization": ""})
        assert response.status_code == 200, response.text
        return response.json()
    monkeypatch.setattr(monitoring_module, "_core_observability", core_observability)
    server = ThreadingHTTPServer(("127.0.0.1", 0), monitoring_module.MonitoringHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # A small node that cannot run the module and the "text" node that can.
    monkeypatch.setitem(store.workers, "tiny-node", {"node_name": "tiny-node", "status": "READY", "role": "text",
                                                     "cpu_cores": 1, "ram_mb": 64})
    manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
    core = api.runtime.modules
    core.report_node("text-node", [{**manifest.to_dict(), "ready": True,
                                    "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}])
    yield {"api": api, "module": monitoring_module, "server": server, "lifecycle": lifecycle,
           "credentials": database, "issued": credentials, "dsn": module_postgres, "core": core, "manifest": manifest}
    server.shutdown()


def _place(monitoring):
    api, core = monitoring["api"], monitoring["core"]
    refused = api.put("/api/nodes/module-registry/placement/monitoring", json={"node_ids": ["tiny-node"]})
    assert refused.status_code == 422 and "RAM" in refused.json()["detail"]
    placed = api.put("/api/nodes/module-registry/placement/monitoring", json={"node_ids": ["text-node"]})
    assert placed.status_code == 200 and placed.json()["node_ids"] == ["text-node"]
    assert api.put("/api/nodes/module-registry/approval/monitoring",
                   json={"permissions": ["monitoring.read"]}).status_code == 200
    assert core.ready_instances("monitoring")


def _get(monitoring, path, role="admin"):
    import json
    connection = HTTPConnection("127.0.0.1", monitoring["server"].server_address[1], timeout=10)
    connection.request("GET", path, headers={"x-vertep-module-token": monitoring["module"].TOKEN, "x-vertep-role": role})
    response = connection.getresponse()
    body = json.loads(response.read() or b"null")
    connection.close()
    return response.status, body


def test_placed_on_a_non_core_node_it_ingests_metrics_health_and_the_alert_history(monitoring, monkeypatch):
    from core.alert_store import get_alert_store
    from core.state import store
    api, module = monitoring["api"], monitoring["module"]
    _place(monitoring)
    assert monitoring["manifest"].placement["target"] == "any"

    # 1) A job fails: the existing CORE collector fires JOB_FAILED.
    from core.models import JobStatus
    job = store.create("Тема", "hero", 5, "web", "image", 0, "brand-a")
    store.update(job, JobStatus.FAILED, "FAILED: render crashed")
    first = module.collect_once()
    assert first["metrics"] > 0 and first["health_checks"] > 0 and first["alert_transitions"] >= 1
    status, alerts = _get(monitoring, "/alerts?active_only=true")
    offline = [a for a in alerts if a["name"] == "JOB_FAILED" and a["labels"].get("job_id") == job.job_id]
    assert status == 200 and len(offline) == 1 and offline[0]["severity"] == "critical"
    core_id = offline[0]["labels"]["core_id"]
    status, metrics = _get(monitoring, "/metrics?name=workers_expected")
    assert status == 200 and metrics and metrics[0]["node_id"] == "core"
    status, by_status = _get(monitoring, "/metrics?name=jobs_by_status")
    assert {m["labels"]["status"] for m in by_status} >= {"NEW"}
    status, health = _get(monitoring, "/health-checks")
    assert status == 200 and {h["component"] for h in health} >= {"core", "core.core_api"}

    # 2) The operator acknowledges it in CORE; 3) the job is retried (recovery).
    assert api.post(f"/api/alerts/{core_id}/acknowledge", json={"actor": "admin"}).status_code == 200
    module.collect_once()
    store.update(job, JobStatus.NEW, "RETRY requested")
    module.collect_once()
    module.collect_once()  # an unchanged state adds no transition
    status, alert = _get(monitoring, f"/alerts/core-{core_id}")
    assert status == 200 and alert["active"] is False and alert["resolved_at"]
    assert alert["labels"]["state"] == "resolved" and alert["labels"]["acknowledged_by"] == "admin"
    status, logs = _get(monitoring, "/logs?component=alerts")
    # Insertion order of the transitions (ids), not timestamps that may coincide.
    history = [entry["context"]["to"] for entry in sorted(logs, key=lambda entry: entry["id"])
               if entry["context"]["alert_id"] == f"core-{core_id}"]
    assert history == ["firing", "acknowledged", "resolved"]
    # CORE stays the source of truth: its own store has the same states.
    assert get_alert_store().list(state="resolved")[0]["id"] == core_id
    status, stats = _get(monitoring, "/stats")
    latest = _get(monitoring, "/metrics?name=workers_online")[1]
    assert status == 200 and stats["online_nodes"] == int(latest[0]["value"]), (stats, latest)

    # Only the Monitoring module with its approved permission reads this API.
    from core.module.gateway import issue_token
    assert api.get("/api/modules/self/observability", headers={"Authorization": ""}).status_code in {401, 403}
    api.runtime.modules.report_node("other-node", [])
    other = api.get("/api/modules/self/observability",
                    headers={"x-vertep-module-token": issue_token("characters", "text-node"), "Authorization": ""})
    assert other.status_code in {401, 403}


def test_uninstall_archives_the_history_restore_reads_it_back_and_core_works_without_it(monitoring, monkeypatch):
    from monitoring_logic import MonitoringModule
    from core.state import store
    api, module = monitoring["api"], monitoring["module"]
    _place(monitoring)
    from core.models import JobStatus
    store.update(store.create("Тема", "hero", 5, "web", "image", 0, "brand-a"), JobStatus.FAILED, "FAILED")
    module.collect_once()
    before = MonitoringModule(monitoring["credentials"])
    alerts_before = sorted((a.id, a.active) for a in before.list_alerts())
    metrics_before = len(before.get_metrics(limit=100000))
    assert alerts_before and metrics_before

    lifecycle = monitoring["lifecycle"]
    archive = lifecycle.uninstall("monitoring", confirmed=True)["archive"]
    assert not lifecycle.database.exists("monitoring")
    # CORE without the module: health, metrics, alerts and acknowledgement still work.
    monitoring["server"].shutdown()
    monitoring["server"].server_close()
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/metrics").json()["workers_expected"] >= 1
    offline = [a for a in api.get("/api/alerts").json() if a["type"] == "JOB_FAILED"]
    assert offline and api.post(f"/api/alerts/{offline[0]['id']}/acknowledge",
                                json={"actor": "admin"}).status_code == 200

    restored = lifecycle.restore_archive(lifecycle.root / "archives" / archive)
    assert restored["state"] == "INSTALLED"
    parsed = urlparse(monitoring["dsn"])  # restore issues a fresh password for the module role
    after = MonitoringModule({**monitoring["issued"]["monitoring"], "host": parsed.hostname, "port": parsed.port})
    assert sorted((a.id, a.active) for a in after.list_alerts()) == alerts_before
    assert len(after.get_metrics(limit=100000)) == metrics_before
    assert after.retention_days() == 3
