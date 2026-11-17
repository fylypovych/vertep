"""i.0.0.4.95 (#495): the demo Application Module through the whole contract.

Disposable real boundaries: a local Docker registry and the module image
pinned by its pushed digest, the module running in its restricted container,
PostgreSQL in a container, CORE (uvicorn) on loopback, the Worker supervisor
and the production Worker task executor. Only the LLM backend is a local stub
(no GPU, no paid or external model).

Flow: install -> placement/approval -> container start by the Worker ->
activate (schema + migration + self-test) -> API/UI -> background service runs
one task on the shared ``text_generation`` capability -> deactivate ->
update (new migration) -> rollback -> uninstall with a verified archive ->
restore from the archive -> data read back.
"""

import base64
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.module_postgres import module_postgres  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
NODE = "demo-node"
TOKEN = "demo-node-token"


def _docker_ready():
    return shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="Docker daemon is unavailable")


def _port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _until(predicate, timeout=120, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    pytest.fail(f"Timed out waiting for {message}")


def _build_module():
    spec = importlib.util.spec_from_file_location("demo_build", ROOT / "examples/modules/demo-notes/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ollama(BaseHTTPRequestHandler):
    prompts = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).prompts.append(body["prompt"])
        data = json.dumps({"response": "SUMMARY<" + body["prompt"] + ">", "done": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def stand(tmp_path, monkeypatch, module_postgres):
    import uvicorn
    from core.app import app
    build = _build_module()
    registry_name, registry_port = f"vertep-demo-registry-{os.getpid()}", _port()
    subprocess.run(["docker", "rm", "-f", registry_name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", registry_name, "-p", f"127.0.0.1:{registry_port}:5000", "registry:2"],
                   check=True, capture_output=True, timeout=300)
    key = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "publishers.json").write_text(json.dumps({"vertep-demo": {
        "keys": {"demo-k1": key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()},
        "repositories": ["github.com/fylypovych/vertep"]}}))
    registry = f"localhost:{registry_port}"
    _until(lambda: subprocess.run(["docker", "exec", registry_name, "true"], capture_output=True).returncode == 0, 60, "registry")
    time.sleep(1)
    packages = {version: build.package(version, build.image(version, registry), key, "demo-k1", "vertep-demo",
                                       "github.com/fylypovych/vertep") for version in ("1.0.0", "1.1.0")}
    llm = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    Ollama.prompts = []
    definitions = tmp_path / "dag"
    definitions.mkdir()
    (definitions / "configured.json").write_text(json.dumps({"name": "Configured", "version": "1.0.0",
                                                            "tasks": [{"id": "script", "type": "text_generation"}]}))
    core_port = _port()
    environment = {
        "WORKFLOW_DEFINITIONS_ROOT": str(definitions), "WORKFLOW_STATE_ROOT": str(tmp_path / "dag-state"),
        "VERTEP_MODULE_DATABASE_URL": module_postgres, "VERTEP_MODULE_DATABASE_HOST": "host.docker.internal",
        "VERTEP_MODULE_TRUST_ROOT": str(trust), "VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lifecycle"),
        "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "placement.json"),
        "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "audit.jsonl"), "VERTEP_MODULE_DRAIN_SECONDS": "5",
        "NODE_API_TOKEN": TOKEN, "NODE_ROLE": "text", "OLLAMA_URL": f"http://127.0.0.1:{llm.server_address[1]}",
        "OLLAMA_MODEL": "demo-model", "LOCAL_WORKER_FALLBACK": "false",
        "VERTEP_APPLICATION_MODULES": "true", "VERTEP_APPLICATION_CORE_URL": f"http://host.docker.internal:{core_port}",
        "VERTEP_APPLICATION_STATE": str(tmp_path / "node-apps.json"), "VERTEP_APPLICATION_PORT_BASE": str(_port()),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("core.system_state.dispatch_allowed", lambda: True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=core_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{core_port}"
    _until(lambda: _ok(f"{base}/api/health"), 60, "CORE")
    probe = subprocess.run(["docker", "run", "--rm", "alpine:3.20", "wget", "-T", "5", "-q", "-O-",
                            f"http://host.docker.internal:{core_port}/api/health"], capture_output=True, timeout=120)
    if probe.returncode != 0:
        server.should_exit = True
        subprocess.run(["docker", "rm", "-f", registry_name], capture_output=True)
        message = "Module containers cannot reach the loopback CORE via host.docker.internal here"
        if os.getenv("VERTEP_REQUIRE_DEMO_MODULE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    admin = httpx.Client(base_url=base, auth=("admin", os.environ["ADMIN_PASSWORD"]), timeout=240)
    node = Node(base)
    try:
        yield {"admin": admin, "base": base, "packages": packages, "node": node, "dsn": module_postgres, "llm": Ollama}
    finally:
        node.stop()
        from worker import application_container
        application_container.stop("demo-notes")
        subprocess.run(["docker", "network", "rm", "vertep-mod-demo-notes"], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "vertep-mod-demo-notes-data"], capture_output=True)
        subprocess.run(["docker", "rm", "-f", registry_name], capture_output=True)
        server.should_exit = True
        admin.close()
        llm.shutdown()


def _ok(url):
    try:
        return httpx.get(url, timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


class Node:
    """The Worker on the demo node: supervisor + heartbeat + production task executor."""

    def __init__(self, core):
        self.core, self.stopped, self.errors, self.executed = core, threading.Event(), [], []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        from worker import application_supervisor
        from worker.service import execute_task, submit_result
        with httpx.Client(headers={"X-Vertep-Token": TOKEN}, timeout=60) as client:
            while not self.stopped.is_set():
                try:
                    inventory = application_supervisor.reconcile(client, self.core, NODE)
                    now = datetime.now(timezone.utc).isoformat()
                    client.post(f"{self.core}/api/workers/heartbeat", json={
                        "node_name": NODE, "role": "text", "status": "FREE", "cpu_cores": 2, "ram_mb": 2048, "vram_mb": 0,
                        "capabilities": ["text_generation"], "module_inventory": inventory,
                        "self_test": {"status": "PASSED", "role": "text", "checked_at": now}}).raise_for_status()
                    claimed = client.post(f"{self.core}/api/tasks/claim/workflow", json={"node_name": NODE}).json().get("task")
                    if claimed:
                        result = execute_task(None, claimed, NODE)
                        result.update(architecture_workflow=True, lease_id=claimed["lease_id"])
                        submit_result(client, self.core, result)
                        self.executed.append(claimed["task_id"])
                except Exception as error:  # surfaced through assertions on outcomes
                    self.errors.append(repr(error))
                time.sleep(1)

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=30)


def _action(admin, action, **body):
    response = admin.post(f"/api/nodes/module-registry/lifecycle/{action}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _notes(admin):
    response = admin.get("/api/modules/demo-notes/v1/notes")
    return response.json() if response.status_code == 200 else None


def _columns(dsn):
    with psycopg.connect(dsn, client_encoding="UTF8") as connection:
        return {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='mod_demo_notes' AND table_name='notes'")}


def _instance(admin):
    instances = admin.get("/api/nodes/module-registry").json()["instances"]
    return next((item for item in instances if item["module_id"] == "demo-notes"), None)


def test_demo_module_full_lifecycle(stand):
    admin, packages, node = stand["admin"], stand["packages"], stand["node"]
    # Install is a real signed package with an image pinned by its pushed digest.
    _until(lambda: NODE in {n["node_id"] for n in admin.get("/api/nodes/module-catalog").json()["nodes"]}, 60, "node report")
    assert _action(admin, "install", package=packages["1.0.0"])["state"] == "INSTALLED"
    assert admin.put("/api/nodes/module-registry/placement/demo-notes", json={"node_ids": [NODE]}).status_code == 200
    assert admin.put("/api/nodes/module-registry/approval/demo-notes", json={"permissions": ["demo-notes.data"]}).status_code == 200
    # The Worker starts the restricted container; it is not READY before activation.
    _until(lambda: (_instance(admin) or {}).get("reasons") == ["lifecycle_installed"], 180, "container report")
    assert _notes(admin) is None
    inspect = json.loads(subprocess.run(["docker", "inspect", "--type", "container", "vertep-mod-demo-notes"], capture_output=True, text=True, check=True).stdout)[0]
    assert inspect["HostConfig"]["ReadonlyRootfs"] and inspect["Config"]["User"] == "65532:65532"
    assert inspect["Config"]["Image"] == packages["1.0.0"]["manifest"]["container"]["image"]

    assert _action(admin, "activate", module_id="demo-notes")["state"] == "ACTIVE"
    _until(lambda: (_instance(admin) or {}).get("state") == "READY", 60, "READY")
    created = admin.post("/api/modules/demo-notes/v1/notes", json={"text": "перша нотатка"})
    assert created.status_code == 201, created.text
    # Background service -> CORE -> Worker (shared text_generation) -> verified result -> own table.
    done = _until(lambda: [n for n in (_notes(admin) or []) if n["status"] == "done"], 120, "summary")
    assert done[0]["summary"] == "SUMMARY<Summarize: перша нотатка>"
    assert node.executed and stand["llm"].prompts == ["Summarize: перша нотатка"]
    ui = admin.get("/api/modules/ui/demo-notes/1.0.0/index.html")
    assert ui.status_code == 200 and ui.headers["content-security-policy"].startswith("sandbox")
    assert httpx.get(f"http://127.0.0.1:{_instance(admin)['endpoint'].rsplit(':', 1)[1]}/notes").status_code == 403  # gateway only

    assert _action(admin, "deactivate", module_id="demo-notes")["state"] == "INACTIVE"
    assert admin.get("/api/modules/demo-notes/v1/notes").status_code == 404
    assert admin.get("/api/health").status_code == 200  # platform keeps working
    _action(admin, "activate", module_id="demo-notes")

    with pytest.raises(AssertionError):
        _action(admin, "update", package=packages["1.1.0"])  # no explicit confirmation
    assert _action(admin, "update", package=packages["1.1.0"], confirm=True)["version"] == "1.1.0"
    assert "reviewed" in _columns(stand["dsn"])
    _until(lambda: (_instance(admin) or {}).get("version") == "1.1.0" and _instance(admin)["state"] == "READY", 120, "1.1.0")
    assert [n["text"] for n in _notes(admin)] == ["перша нотатка"]

    assert _action(admin, "rollback", module_id="demo-notes")["version"] == "1.0.0"
    assert "reviewed" not in _columns(stand["dsn"])
    _until(lambda: (_instance(admin) or {}).get("version") == "1.0.0" and _instance(admin)["state"] == "READY", 120, "rollback")
    assert [n["summary"] for n in _notes(admin)] == ["SUMMARY<Summarize: перша нотатка>"]

    uninstalled = _action(admin, "uninstall", module_id="demo-notes", confirm=True)
    assert admin.get("/api/nodes/module-registry/lifecycle").json()["modules"] == []
    _until(lambda: subprocess.run(["docker", "inspect", "--type", "container", "vertep-mod-demo-notes"],
                                  capture_output=True).returncode != 0, 60, "container stop")
    assert _columns(stand["dsn"]) == set()
    archive = admin.get(f"/api/nodes/module-registry/archives/{uninstalled['archive']}")
    assert archive.status_code == 200 and archive.content[:100]  # admin download of the plain tar

    restored = admin.post(f"/api/nodes/module-registry/archives/{uninstalled['archive']}/restore")
    assert restored.status_code == 200 and restored.json()["state"] == "INSTALLED"
    _action(admin, "activate", module_id="demo-notes")
    _until(lambda: (_notes(admin) or [{}])[0].get("summary") == "SUMMARY<Summarize: перша нотатка>", 120, "restored data")
    events = admin.get("/api/nodes/module-registry/audit").json()["events"]
    assert {"module.task", "module.database", "module.lifecycle", "module.api"} <= {e["action"] for e in events}
