"""i.0.0.4.93 (#493): module trust, isolation, permissions and API/UI extensions.

Real boundaries used here: Ed25519 signatures over exact bytes, the CORE
FastAPI app with its auth middleware, a real HTTP module process on a socket
for the gateway, the encrypted platform secret store, and (when a Docker
daemon is reachable) a real restricted container.
"""

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.manifest import ModuleManifest
from core.module.packages import ModulePackages
from core.module.trust import Publisher, TrustError, TrustStore

DIGEST = "sha256:" + "b" * 64
CONTAINER = {"image": "registry.example/vertep/app@" + DIGEST, "port": 8080}
UI_HTML = b"<!doctype html><html><body><h1 id='ext'>Extension OK</h1><script>document.title='ext'</script></body></html>"


def pem(key):
    return key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def sign(key, manifest, files=None, signer="acme-k1"):
    files = files or {"module.py": base64.b64encode(b"# app code is never imported\n").decode()}
    content = {"manifest": manifest, "files": files}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {**content, "signer": signer, "signature": base64.b64encode(key.sign(raw)).decode()}


def app_manifest(name="notes", **extra):
    base = {"schema_version": "2.0", "name": name, "version": "1.0.0", "capabilities": [], "kind": "application",
            "publisher": "acme", "provenance": {"repository": "github.com/acme/notes"}, "container": CONTAINER}
    base.update(extra)
    return base


@pytest.fixture
def acme(tmp_path):
    key = Ed25519PrivateKey.generate()
    trust = TrustStore({"acme": Publisher("acme", {"acme-k1": pem(key)}, ["github.com/acme/notes"])})
    return key, ModulePackages(tmp_path / "installed", trust, {"core_runtime": "0.0.3.88"})


# -- AC1: signature, content, issuer, provenance, forks ------------------------------

def test_trusted_publisher_package_installs(acme):
    key, packages = acme
    ui = {"entry": "index.html", "files": {"index.html": hashlib.sha256(UI_HTML).hexdigest()}}
    files = {"module.py": base64.b64encode(b"x").decode(), "index.html": base64.b64encode(UI_HTML).decode()}
    assert packages.install(sign(key, app_manifest(ui=ui), files))["name"] == "notes"


@pytest.mark.parametrize("case", ["tampered_file", "tampered_manifest", "wrong_issuer", "fork_with_upstream_key",
                                  "untrusted_signer", "ui_digest", "legacy_key_v2", "core_incompatible"])
def test_untrusted_or_tampered_packages_are_rejected(acme, tmp_path, case):
    key, packages = acme
    package = sign(key, app_manifest())
    if case == "tampered_file":
        package["files"]["module.py"] = base64.b64encode(b"evil").decode()
    elif case == "tampered_manifest":
        package["manifest"]["version"] = "1.0.1"
    elif case == "wrong_issuer":
        package = sign(key, app_manifest(publisher="other"))
    elif case == "fork_with_upstream_key":
        package = sign(key, app_manifest(provenance={"repository": "github.com/mallory/notes",
                                                     "forked_from": "github.com/acme/notes"}))
    elif case == "untrusted_signer":
        package = sign(Ed25519PrivateKey.generate(), app_manifest(), signer="mallory")
    elif case == "ui_digest":
        ui = {"entry": "index.html", "files": {"index.html": "0" * 64}}
        package = sign(key, app_manifest(ui=ui), {"index.html": base64.b64encode(UI_HTML).decode()})
    elif case == "legacy_key_v2":
        legacy = ModulePackages(tmp_path / "legacy", {"acme-k1": pem(key)}, {"core_runtime": "0.0.3.88"})
        with pytest.raises(TrustError, match="Legacy key"):
            legacy.install(package)
        return
    elif case == "core_incompatible":
        package = sign(key, app_manifest(core_compatibility={"min": "0.0.9.0"}))
        with pytest.raises(ValueError, match="dependencies"):
            packages.install(package)
        assert packages.list() == []
        return
    with pytest.raises(ValueError):
        packages.install(package)
    assert packages.list() == []


def test_fork_is_trusted_only_through_its_own_publisher_record(tmp_path):
    upstream, fork = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    root = tmp_path / "trust"
    root.mkdir()
    (root / "publishers.json").write_text(json.dumps({
        "acme": {"keys": {"acme-k1": pem(upstream).decode()}, "repositories": ["github.com/acme/notes"]}}))
    manifest = app_manifest(publisher="mallory", provenance={"repository": "github.com/mallory/notes",
                                                             "forked_from": "github.com/acme/notes"})
    package = sign(fork, manifest, signer="mallory-k1")
    with pytest.raises(TrustError, match="not trusted"):
        ModulePackages(tmp_path / "a", TrustStore.from_root(root), {"core_runtime": "0.0.3.88"}).install(package)
    # The administrator explicitly trusts the fork's own publisher.
    data = json.loads((root / "publishers.json").read_text())
    data["mallory"] = {"keys": {"mallory-k1": pem(fork).decode()}, "repositories": ["github.com/mallory/notes"]}
    (root / "publishers.json").write_text(json.dumps(data))
    assert ModulePackages(tmp_path / "b", TrustStore.from_root(root), {"core_runtime": "0.0.3.88"}).install(package)


# -- AC5: permission approval; access roles are untouched -----------------------------

def test_permissions_need_admin_approval_and_expansion_needs_new_approval(tmp_path):
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.3.88"}, PlacementStore(tmp_path / "p.json"))
    report = lambda permissions: registry.report_node("n", [{**app_manifest(permissions=permissions),
                                                             "ready": True, "endpoint": "http://127.0.0.1:9"}])
    report(["notes.read"])
    registry.set_placement("notes", ["n"])
    assert registry.ready_instances("notes") == []
    assert "permission_approval_required" in registry.instances("notes")[0].reasons
    with pytest.raises(ValueError, match="declares"):
        registry.approve_permissions("notes", ["notes.read", "core.admin"])
    registry.approve_permissions("notes", ["notes.read"])
    assert len(registry.ready_instances("notes")) == 1
    report(["notes.read", "notes.write"])  # update widens permissions
    assert registry.ready_instances("notes") == []
    registry.approve_permissions("notes", ["notes.read", "notes.write"])
    assert len(registry.ready_instances("notes")) == 1
    from core.security import SESSION_ROLES
    assert SESSION_ROLES == ("admin", "moderator")  # access roles are not deployment roles


# -- AC2: application code is not imported; restricted container ----------------------

def test_application_code_never_loads_in_process(acme, monkeypatch, tmp_path):
    key, packages = acme
    packages.install(sign(key, app_manifest()))
    from worker.module_runtime import load_module
    with pytest.raises(ValueError, match="own container"):
        load_module(packages, "notes")
    import ast
    from pathlib import Path
    def own_code(argument):
        # Only constant names or CORE's own packages may be imported dynamically.
        if isinstance(argument, ast.Constant):
            return True
        first = argument.values[0] if isinstance(argument, ast.JoinedStr) and argument.values else None
        return isinstance(first, ast.Constant) and str(first.value).startswith("core.")
    for path in Path("core").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if isinstance(node.func, ast.Name) and name in {"exec", "eval", "compile"}:
                pytest.fail(f"{path}: CORE executes dynamic code")
            if name in {"__import__", "import_module"} and node.args and not own_code(node.args[0]):
                pytest.fail(f"{path}: CORE imports a non-CORE module dynamically")


def test_container_arguments_are_restricted_and_tamper_checked():
    from worker.application_container import run_arguments, validate_arguments
    manifest = ModuleManifest.from_dict(app_manifest(resource_requirements={"min_ram_mb": 256, "min_cpu_cores": 1, "min_vram_mb": 0}))
    arguments = run_arguments(manifest, core_url="https://core:8080", module_token="t", host_port=18080)
    joined = " ".join(arguments)
    for required in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65532:65532",
                     "--network=vertep-mod-notes", "src=vertep-mod-notes-data,dst=/data", CONTAINER["image"]):
        assert required in joined
    for forbidden in ("docker.sock", "--privileged", "type=bind", "DATABASE", "REDIS", "POSTGRES", "-v "):
        assert forbidden not in joined
    for extra in (["-v", "/var/run/docker.sock:/var/run/docker.sock"], ["--privileged"], ["--network=host"],
                  ["--cap-add=SYS_ADMIN"], ["--mount=type=bind,src=/,dst=/host"], ["--pid=host"]):
        with pytest.raises(ValueError, match="Forbidden"):
            validate_arguments(arguments[:-1] + extra + arguments[-1:])
    with pytest.raises(ValueError, match="mandatory"):
        validate_arguments([a for a in arguments if a != "--read-only"])
    execution = ModuleManifest("text", "1.0.0", [], [], {})
    with pytest.raises(ValueError):
        run_arguments(execution, core_url="x", module_token="t", host_port=1)


def _docker_ready():
    return shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="Docker daemon is unavailable")
def test_real_container_boundary(tmp_path):
    """Two module containers from a digest-pinned image on a real Docker daemon."""
    from worker import application_container as runner
    subprocess.run(["docker", "pull", "-q", "alpine:3.20"], check=True, capture_output=True, timeout=300)
    image = subprocess.run(["docker", "image", "inspect", "alpine:3.20", "--format", "{{index .RepoDigests 0}}"],
                           check=True, capture_output=True, text=True).stdout.strip()
    modules = {}
    try:
        for index, name in enumerate(("bnd-a", "bnd-b")):
            manifest = ModuleManifest.from_dict(app_manifest(name, container={"image": image, "port": 8080}))
            runner.stop(manifest.module_id)
            runner.start(manifest, core_url="https://core.invalid", module_token="token-" + name,
                         host_port=28080 + index, command=["nc", "-lk", "-p", "8080", "-e", "echo", "module-" + name])
            modules[name] = manifest
        assert runner.running("bnd-a") and runner.running("bnd-b")

        def run(name, script):
            return subprocess.run(["docker", "exec", runner.resource_name(name), "sh", "-c", script],
                                  capture_output=True, text=True, timeout=60)
        assert run("bnd-a", "id -u").stdout.strip() == "65532"
        assert run("bnd-a", "touch /root-write").returncode != 0  # read-only root filesystem
        assert run("bnd-a", "test -e /var/run/docker.sock").returncode != 0
        assert run("bnd-a", "grep CapEff /proc/self/status").stdout.split()[-1] == "0000000000000000"
        mounts = run("bnd-a", "cat /proc/mounts").stdout
        assert " /data " in mounts and " /host " not in mounts and "docker.sock" not in mounts
        environment = run("bnd-a", "env").stdout
        assert "VERTEP_MODULE_ID=bnd-a" in environment
        assert not any(word in environment for word in ("DATABASE", "REDIS", "POSTGRES", "token-bnd-b"))
        inspect = json.loads(subprocess.run(["docker", "inspect", runner.resource_name("bnd-a")],
                                            capture_output=True, text=True, check=True).stdout)[0]
        assert list(inspect["NetworkSettings"]["Networks"]) == ["vertep-mod-bnd-a"]
        assert inspect["HostConfig"]["Privileged"] is False and inspect["HostConfig"]["ReadonlyRootfs"] is True
        assert all(m["Type"] == "volume" for m in inspect["Mounts"])
        address = inspect["NetworkSettings"]["Networks"]["vertep-mod-bnd-a"]["IPAddress"]
        # Another module's container is on a different network and cannot reach it.
        # The listener works inside its own network namespace ...
        assert "module-bnd-a" in run("bnd-a", "nc -w 3 127.0.0.1 8080 </dev/null").stdout
        # Known Docker behaviour: a *published* port is reachable from other bridge
        # networks. Network separation is therefore not the trust boundary; the
        # module authenticates the CORE gateway token (VERTEP_MODULE_TOKEN), which
        # another module does not hold (asserted above via env isolation).
        assert run("bnd-b", "env").stdout.count("VERTEP_MODULE_TOKEN=token-bnd-b") == 1
    finally:
        for name in modules:
            runner.stop(name)
            subprocess.run(["docker", "network", "rm", runner.resource_name(name)], capture_output=True)
            subprocess.run(["docker", "volume", "rm", runner.resource_name(name) + "-data"], capture_output=True)


# -- AC3/AC4/AC6: gateway, secrets, UI, audit through the CORE app ------------------------

class ModuleHandler(BaseHTTPRequestHandler):
    seen = []
    ui = UI_HTML

    def _reply(self):
        type(self).seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.startswith("/ui/"):
            body, kind = type(self).ui, "text/html"
        else:
            body, kind = json.dumps({"path": self.path, "user": self.headers.get("x-vertep-user"),
                                     "caller": self.headers.get("x-vertep-caller-module")}).encode(), "application/json"
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import app
    from core.module import instances
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModuleHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    ModuleHandler.seen, ModuleHandler.ui = [], UI_HTML
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.3.88"}, PlacementStore(tmp_path / "p.json"))

    class Runtime:
        modules = registry
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: Runtime)
    from core.state import store
    for node in ("app-node", "lab-vm"):  # nodes CORE knows from their Worker reports
        monkeypatch.setitem(store.workers, node, {"node_name": node, "role": "core", "status": "READY"})
    ui = {"entry": "index.html", "navigation": [{"id": "notes", "label": "Нотатки"}],
          "files": {"index.html": hashlib.sha256(UI_HTML).hexdigest()}}
    notes = {**app_manifest("notes", api={"namespace": "notes", "version": "1.3", "routes": ["/items", "/items/{id}"]},
                            provides=[{"type": "api", "name": "notes", "version": "1.3"}], ui=ui,
                            permissions=["notes.read"], secret_refs=[{"name": "api_key", "ref": "secret://notes/api_key"}]),
             "ready": True, "endpoint": endpoint}
    client_ok = {**app_manifest("reports", provenance={"repository": "github.com/acme/notes"},
                                requires=[{"type": "api", "name": "notes", "min_version": "1.0", "max_version": "1.9"}],
                                secret_refs=[{"name": "own", "ref": "secret://reports/own"}]),
                 "ready": True, "endpoint": endpoint}
    stranger = {**app_manifest("stranger"), "ready": True, "endpoint": endpoint}
    lab = {**app_manifest("lab", placement={"target": "isolated_lab"},
                          secret_refs=[{"name": "grant", "ref": "secret://lab/grant"}]), "ready": True, "endpoint": endpoint}
    registry.report_node("app-node", [notes, client_ok, stranger])
    registry.report_node("lab-vm", [lab])
    registry.set_node_profile("lab-vm", "nonproduction", "vm")
    for module_id, node in (("notes", "app-node"), ("reports", "app-node"), ("stranger", "app-node"), ("lab", "lab-vm")):
        registry.set_placement(module_id, [node])
    registry.approve_permissions("notes", ["notes.read"])
    client = TestClient(app)
    yield {"client": client, "registry": registry, "endpoint": endpoint, "notes": notes}
    server.shutdown()


def token(module_id, node):
    from core.module.gateway import issue_token
    return issue_token(module_id, node)


def events(tmp_path_file=None):
    from core.module.gateway import read_audit
    return read_audit()


def test_user_api_calls_are_namespaced_versioned_and_declared_only(gateway):
    client = gateway["client"]
    ok = client.get("/api/modules/notes/v1/items/42", headers={"Cookie": "x=1"})
    assert ok.status_code == 200 and ok.json()["user"] == "admin" and ok.json()["path"] == "/items/42"
    forwarded = ModuleHandler.seen[-1]["headers"]
    assert "cookie" not in forwarded and "authorization" not in forwarded
    assert forwarded["x-vertep-module-token"] == token("notes", "app-node")
    assert client.get("/api/modules/notes/v2/items").status_code == 404  # undeclared major
    assert client.get("/api/modules/notes/v1/admin/drop").status_code == 404  # undeclared route
    assert client.get("/api/modules/ghost/v1/items").status_code == 404
    assert len(ModuleHandler.seen) == 1  # denials never reach the module


def test_module_to_module_only_through_declared_api(gateway):
    client = gateway["client"]
    allowed = client.get("/api/modules/notes/v1/items", headers={"x-vertep-module-token": token("reports", "app-node")})
    assert allowed.status_code == 200 and allowed.json()["caller"] == "reports"
    denied = client.get("/api/modules/notes/v1/items", headers={"x-vertep-module-token": token("stranger", "app-node")})
    assert denied.status_code == 403
    forged = client.get("/api/modules/notes/v1/items", headers={"x-vertep-module-token": "Zm9yZ2Vk"})
    assert forged.status_code == 401
    wrong_node = client.get("/api/modules/notes/v1/items", headers={"x-vertep-module-token": token("reports", "lab-vm")})
    assert wrong_node.status_code == 401


def test_secrets_are_scoped_to_own_declared_references(gateway):
    client = gateway["client"]
    assert client.put("/api/nodes/module-registry/secrets/notes", json={"secrets": {"api_key": "notes-secret"}}).status_code == 200
    assert client.put("/api/nodes/module-registry/secrets/reports", json={"secrets": {"own": "reports-secret"}}).status_code == 200
    assert client.put("/api/nodes/module-registry/secrets/reports", json={"secrets": {"api_key": "x"}}).status_code == 422
    assert client.put("/api/nodes/module-registry/secrets/lab", json={"secrets": {"grant": "lab-grant"}}).status_code == 200
    mine = client.get("/api/modules/self/secrets", headers={"x-vertep-module-token": token("reports", "app-node")}).json()
    assert mine == {"secrets": {"own": "reports-secret"}}
    lab = client.get("/api/modules/self/secrets", headers={"x-vertep-module-token": token("lab", "lab-vm")}).json()
    assert lab == {"secrets": {"grant": "lab-grant"}}  # LAB gets no production or foreign secret
    from core.first_run import ensure_secret_store
    platform = {key for key in ensure_secret_store() if not key.startswith("module:")}
    for body in (mine, lab):
        assert not platform & set(body["secrets"]) and "notes-secret" not in json.dumps(body)
    assert client.get("/api/modules/self/secrets").status_code == 401  # a user session is not a module


def test_unapproved_or_deactivated_module_loses_api_secrets_and_ui(gateway):
    client, registry = gateway["client"], gateway["registry"]
    assert client.get("/api/modules/notes/v1/items").status_code == 200
    assert [e["module_id"] for e in client.get("/api/modules/ui-extensions").json()["extensions"]] == ["notes"]
    registry.approve_permissions("notes", [])  # approval revoked -> lifecycle deactivates access
    assert client.get("/api/modules/notes/v1/items").status_code == 404
    assert client.get("/api/modules/ui-extensions").json()["extensions"] == []
    assert client.get("/api/modules/ui/notes/1.0.0/index.html").status_code == 404
    assert client.get("/api/modules/self/secrets", headers={"x-vertep-module-token": token("notes", "app-node")}).status_code == 401


def test_signed_ui_is_digest_checked_and_sandboxed(gateway):
    client = gateway["client"]
    ok = client.get("/api/modules/ui/notes/1.0.0/index.html", headers={"Authorization": ""})
    assert ok.status_code == 200 and ok.content == UI_HTML
    assert ok.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in ok.headers["content-security-policy"]
    assert ok.headers["x-frame-options"] == "SAMEORIGIN"
    assert client.get("/api/modules/ui/notes/1.0.0/other.js").status_code == 404  # not a signed file
    ModuleHandler.ui = b"<script>steal()</script>"
    assert client.get("/api/modules/ui/notes/1.0.0/index.html").status_code == 502
    assert client.get("/api/health").status_code == 200  # the main panel API keeps working


def test_admin_only_governance_and_complete_audit(gateway, monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import app
    from core.security import _session_token
    client = gateway["client"]
    assert client.put("/api/nodes/module-registry/approval/notes", json={"permissions": ["notes.read"]}).status_code == 200
    assert client.put("/api/nodes/module-registry/placement/notes", json={"node_ids": ["app-node"]}).status_code == 200
    client.get("/api/modules/notes/v1/items")
    client.get("/api/modules/notes/v1/nope")
    client.get("/api/modules/ui/notes/1.0.0/index.html")
    monkeypatch.setenv("USERS_JSON", json.dumps({"reader": {"password_hash": "x", "role": "moderator"}}))
    session = _session_token("reader", "moderator")
    from core.security import session_secret
    moderator = TestClient(app, headers={"Authorization": ""})
    moderator.cookies.set("vertep_session", session)
    csrf = hmac.new(session_secret().encode() if isinstance(session_secret(), str) else session_secret(),
                    session.encode(), hashlib.sha256).hexdigest()
    moderator.cookies.set("vertep_csrf", csrf)
    for path, body in (("/api/nodes/module-registry/approval/notes", {"permissions": []}),
                       ("/api/nodes/module-registry/secrets/notes", {"secrets": {"api_key": "x"}}),
                       ("/api/nodes/module-registry/placement/notes", {"node_ids": []})):
        assert moderator.put(path, json=body, headers={"x-csrf-token": csrf}).status_code == 403
    assert moderator.get("/api/nodes/module-registry/audit").status_code == 403
    assert moderator.get("/api/modules/notes/v1/items").json()["user"] == "reader"  # same Vertep identity
    trail = client.get("/api/nodes/module-registry/audit").json()["events"]
    outcomes = {(e["action"], e["outcome"], e.get("reason")) for e in trail}
    assert ("module.api", "allowed", None) in outcomes
    assert ("module.api", "denied", "undeclared_route") in outcomes
    assert ("module.ui", "allowed", None) in outcomes  # page-open probes are recorded too
    assert ("module.approval", "allowed", None) in outcomes and ("module.placement", "allowed", None) in outcomes
    assert not any("notes-secret" in json.dumps(e) for e in trail)


def test_lab_is_refused_on_a_production_node_and_gets_no_database_both_audited(gateway):
    """#523 AC1/AC3: the HTTP placement of the LAB on a production node is refused and
    audited; with its own module token the LAB gets no production database."""
    client = gateway["client"]
    refused = client.put("/api/nodes/module-registry/placement/lab", json={"node_ids": ["app-node"]})
    assert refused.status_code == 422 and "nonproduction" in refused.json()["detail"]
    database = client.get("/api/modules/self/database", headers={"x-vertep-module-token": token("lab", "lab-vm")})
    assert database.status_code == 404
    trail = client.get("/api/nodes/module-registry/audit").json()["events"]
    placement = [e for e in trail if e["action"] == "module.placement" and e["outcome"] == "denied"]
    assert placement and placement[-1]["module"] == "lab" and "nonproduction" in placement[-1]["reason"]
    assert any(e["action"] == "module.database" and e["outcome"] == "denied" and e.get("module") == "lab"
               and e.get("reason") == "no_module_schema" for e in trail)
    assert set(gateway["registry"].dispatch_targets("lab")) == {"lab-vm"}  # the refusal changed nothing


def test_worker_token_issue_requires_placement_approval_and_worker_auth(gateway, monkeypatch):
    client = gateway["client"]
    monkeypatch.setenv("NODE_API_TOKEN", "node-token")
    headers = {"x-vertep-token": "node-token", "Authorization": ""}
    issued = client.post("/api/modules/token", json={"node_name": "app-node", "module_id": "notes"}, headers=headers)
    assert issued.status_code == 200 and issued.json()["token"] == token("notes", "app-node")
    assert client.post("/api/modules/token", json={"node_name": "lab-vm", "module_id": "notes"}, headers=headers).status_code == 403
    assert client.post("/api/modules/token", json={"node_name": "app-node", "module_id": "notes"},
                       headers={"x-vertep-token": "bad", "Authorization": ""}).status_code == 401
    gateway["registry"].approve_permissions("notes", [])
    assert client.post("/api/modules/token", json={"node_name": "app-node", "module_id": "notes"}, headers=headers).status_code == 403
