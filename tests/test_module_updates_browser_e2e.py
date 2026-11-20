"""#515: the Updates panel in a real browser against a real CORE.

CORE runs on a socket with its module lifecycle, sources, Update Manager and the
production panel. Only the outside world is simulated: the https Module Registry
(fetch transport) and one node that behaves like the Worker supervisor — it takes
its packages from CORE's real ``node-assignments`` and reports what it runs.
The admin sees a recommendation, plans, approves and runs it through the Manager,
watches the progress, opens the linked UPDATE journal and rolls back to a point.
"""

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_media_browser_e2e import WORKER_TOKEN, browser, core, expect  # noqa: F401  (fixtures)
from tests.test_module_trust_isolation import CONTAINER, pem, sign

REGISTRY = "https://registry.example/vertep/index.json"


class Health(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"{}")


def test_recommend_approve_run_progress_journal_and_rollback_in_the_panel(core, browser, monkeypatch, tmp_path):  # noqa: F811
    import core.module.sources as sources
    key = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "publishers.json").write_text(json.dumps({"acme": {"keys": {"acme-k1": pem(key).decode()},
                                                                 "repositories": ["github.com/acme/notes"]}}))
    for name, value in {"VERTEP_MODULE_TRUST_ROOT": str(trust), "VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lc"),
                        "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "placement.json"),
                        "WORKFLOW_STATE_ROOT": str(tmp_path / "wf"), "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "a.jsonl"),
                        "DATABASE_URL": "", "VERTEP_MODULE_DATABASE_URL": "", "VERTEP_MODULE_DRAIN_SECONDS": "1"}.items():
        monkeypatch.setenv(name, value)
    network = {}

    def publish(*versions):
        entries = []
        for version in versions:
            manifest = {"schema_version": "2.0", "name": "notes", "version": version, "capabilities": [],
                        "kind": "application", "publisher": "acme", "provenance": {"repository": "github.com/acme/notes"},
                        "container": CONTAINER, "core_compatibility": {"min": "0.0.4.0", "max": "0.0.9.99"},
                        "migrations": []}
            data = json.dumps(sign(key, manifest, {})).encode()
            network[f"https://registry.example/vertep/notes-{version}.pkg"] = data
            entries.append({"version": version, "channel": "stable", "package_url": f"notes-{version}.pkg",
                            "sha256": hashlib.sha256(data).hexdigest()})
        network[REGISTRY] = json.dumps({"format_version": 1, "modules": {"notes": entries}}).encode()

    def fetch(url):
        if url not in network:
            raise sources.SourceError(f"404 {url}")
        return network[url]
    monkeypatch.setattr(sources.SourceResolver.__init__, "__defaults__", (fetch,))

    health = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{health.server_address[1]}"
    api, url = core["api"], core["url"]
    stop = threading.Event()

    def node():
        """The node, like the Worker supervisor: heartbeat, assignments from CORE, report."""
        from core.module.gateway import _modules_or_none
        worker = httpx.Client(base_url=url, headers={"x-vertep-token": WORKER_TOKEN}, timeout=10)
        while not stop.is_set():
            try:
                worker.post("/api/workers/heartbeat", json={"node_name": "n1", "role": "text", "status": "FREE",
                                                            "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192,
                                                            "capabilities": ["text_generation"]})
                assigned = worker.post("/api/modules/node-assignments", json={"node_name": "n1"}).json()
                registry = _modules_or_none()
                if registry is not None:
                    registry.report_node("n1", [{**item["package"]["manifest"], "ready": True, "endpoint": endpoint}
                                                for item in assigned.get("modules", [])])
            except (httpx.HTTPError, ValueError):
                pass
            stop.wait(0.3)
    thread = threading.Thread(target=node, daemon=True)
    thread.start()
    try:
        publish("0.0.0.1")
        installed = api.post("/api/nodes/module-registry/sources/install",
                             json={"source": REGISTRY, "module_id": "notes", "approved_profile": "minimal"})
        assert installed.status_code == 200, installed.text
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            placed = api.put("/api/nodes/module-registry/placement/notes", json={"node_ids": ["n1"]})
            if placed.status_code == 200:
                break
            time.sleep(0.3)
        assert placed.status_code == 200, placed.text
        assert api.put("/api/nodes/module-registry/approval/notes", json={"permissions": []}).status_code == 200
        activated = api.post("/api/nodes/module-registry/lifecycle/activate", json={"module_id": "notes"})
        assert activated.status_code == 200, activated.text
        publish("0.0.0.1", "0.0.0.2")  # a newer stable release appears in the pinned source

        page = browser["page"]
        page.goto(f"{url}/settings?tab=update")
        row = page.get_by_test_id("update-recommendation-notes")
        expect(row).to_contain_text("0.0.0.1")
        expect(row).to_contain_text("0.0.0.2")
        expect(page.get_by_test_id("update-risk-notes")).to_contain_text("low")
        page.get_by_test_id("update-plan-notes").click()
        expect(page.get_by_test_id("update-plan-status").first).to_have_text("PENDING_APPROVAL")
        page.get_by_test_id("update-approve").click()
        expect(page.get_by_test_id("update-plan-status").first).to_have_text("APPROVED")
        page.get_by_test_id("update-execute").click()
        expect(page.get_by_test_id("update-plan-status").first).to_have_text("DONE", timeout=60_000)
        expect(page.get_by_test_id("update-node-notes-n1")).to_contain_text("DONE")
        page.get_by_test_id("update-show-log").first.click()
        journal = page.get_by_test_id("update-log")
        # /api/logs reads the whole journal directory; under a parallel suite it can be slow.
        expect(journal).to_contain_text("UPDATE plan created", timeout=30_000)
        expect(journal).to_contain_text("UPDATE approved", timeout=30_000)
        expect(journal).to_contain_text("UPDATE module ready", timeout=30_000)
        plan_id = api.get("/api/nodes/module-registry/updates").json()["plans"][-1]["id"]
        records = api.get(f"/api/logs?category=UPDATE&operation_id={plan_id}").json()
        assert records and all(r["operation_id"] == plan_id and r["category"] == "UPDATE" for r in records)

        page.get_by_test_id("rollback-points-notes").click()
        point = page.get_by_test_id("rollback-point-0.0.0.1")
        expect(point).to_be_visible()
        page.get_by_test_id("rollback-accept-loss").check()
        with page.expect_response(lambda r: r.url.endswith("/lifecycle/rollback_point")) as answer:
            page.get_by_test_id("rollback-to-point").click()
        assert answer.value.ok, answer.value.text()
        catalog = api.get("/api/nodes/module-catalog").json()["modules"]
        assert next(m for m in catalog if m["module_id"] == "notes")["version"] == "0.0.0.1"
    finally:
        stop.set()
        thread.join(timeout=5)
        health.shutdown()
