"""i.0.0.4.72 (#472): node identity reaches CORE only from a verified TLS peer.

A disposable acceptance on a real nginx running the shipped ``deploy/proxy.conf``
in front of a real ``uvicorn core.app:app`` process with ``NODE_MTLS_REQUIRED=true``
and a node certificate issued by that CORE. A valid worker token plus forged
``X-Vertep-Client-DN`` / ``X-Vertep-Client-Serial`` headers without a client
certificate is refused on every worker route; the certificate + token path works;
Web/setup routes keep working without a client certificate.

The nginx binary is taken from ``VERTEP_NGINX_EXECUTABLE`` or ``PATH``. The test
skips when nginx or openssl is unavailable unless ``VERTEP_REQUIRE_PROXY_ACCEPTANCE=1``.
Production PKI/stand evidence stays with #54.
"""

import base64
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = "proxy-node"
OTHER = "other-node"
ADMIN_PASSWORD = "proxy-identity-test-password"
AUTH = "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(check, message, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.1)
    pytest.fail(f"{message} did not become ready")


def _require(tool: str | None, name: str) -> str:
    if tool:
        return tool
    if os.getenv("VERTEP_REQUIRE_PROXY_ACCEPTANCE") == "1":
        pytest.fail(f"{name} is required for the proxy identity acceptance")
    pytest.skip(f"{name} is unavailable; proxy identity acceptance is BLOCKED")


def test_shipped_configs_never_forward_unverified_identity():
    """Every location of both shipped configs takes DN/Serial from the verified peer."""
    for name in ("deploy/proxy.conf", "deploy/proxy.conf.template"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "map $ssl_client_verify $vertep_client_dn { SUCCESS $ssl_client_s_dn; default \"\"; }" in text
        assert "map $ssl_client_verify $vertep_client_serial { SUCCESS $ssl_client_serial; default \"\"; }" in text
        assert text.count("proxy_pass http://core:8080;") == 2
        assert text.count("proxy_set_header X-Vertep-Client-DN $vertep_client_dn;") == 2
        assert text.count("proxy_set_header X-Vertep-Client-Serial $vertep_client_serial;") == 2
        assert "X-Vertep-Client-DN $ssl_client_s_dn" not in text
        assert "X-Vertep-Client-Serial $ssl_client_serial" not in text


class _EchoUpstream:
    """Stands in for CORE behind the proxy and records the identity headers it receives."""

    def __init__(self):
        self.seen: dict[str, dict] = {}
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                upstream.seen[self.path] = {
                    "dn": self.headers.get("x-vertep-client-dn"),
                    "serial": self.headers.get("x-vertep-client-serial")}
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = _answer

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _start_nginx(nginx: str, prefix: Path, tls: Path, crl: Path, upstream: str):
    """Run the shipped deploy/proxy.conf unchanged except for paths, port and upstream."""
    port = _free_port()
    site = (ROOT / "deploy/proxy.conf").read_text(encoding="utf-8")
    site = (site.replace("listen 8443 ssl;", f"listen 127.0.0.1:{port} ssl;")
            .replace("/etc/vertep/tls/", f"{tls}/")
            .replace("/etc/vertep/revocation/node-ca.crl", str(crl))
            .replace("http://core:8080", upstream))
    (prefix / "logs").mkdir(parents=True)
    (prefix / "site.conf").write_text(site, encoding="utf-8")
    temp = "\n".join(f"  {name}_temp_path {prefix / name};"
                     for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
    (prefix / "nginx.conf").write_text(
        f"pid {prefix}/nginx.pid;\nerror_log {prefix}/logs/error.log;\nevents {{}}\nhttp {{\n"
        f"  access_log {prefix}/logs/access.log;\n{temp}\n  include {prefix}/site.conf;\n}}\n",
        encoding="utf-8")
    log = (prefix / "stdout.log").open("w", encoding="utf-8")
    process = subprocess.Popen([nginx, "-p", str(prefix), "-c", str(prefix / "nginx.conf"),
                                "-g", "daemon off;"], stdout=log, stderr=subprocess.STDOUT)
    url = f"https://127.0.0.1:{port}"

    def ready():
        assert process.poll() is None, (prefix / "logs/error.log").read_text(encoding="utf-8")
        try:
            httpx.get(url + "/api/health", verify=False, timeout=3)
            return True
        except httpx.HTTPError:
            return False
    _wait(ready, "nginx")
    return process, url


@pytest.fixture
def stack(tmp_path):
    nginx = _require(os.getenv("VERTEP_NGINX_EXECUTABLE") or shutil.which("nginx"), "nginx")
    openssl = _require(shutil.which("openssl"), "openssl")
    env = os.environ.copy()
    for name in ("CONFIG_ROOT", "JOB_ROOT", "STORAGE_ROOT", "UPDATE_STATE_DIR", "LOG_ROOT"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        env[name] = str(directory)
    config = Path(env["CONFIG_ROOT"])
    (config / "users.json").write_text("{}", encoding="utf-8")
    (config / "installation.json").write_text('{"completed_at":"2026-01-01"}', encoding="utf-8")
    env.update(NODE_MTLS_REQUIRED="true", NODE_CA_CERT_PATH=str(config / "pki/ca.crt"),
               NODE_CA_KEY_PATH=str(config / "pki/ca.key"), NODE_API_TOKEN="",
               LOCAL_WORKER_FALLBACK="false", SYSTEM_STATE_BACKEND="file",
               ADMIN_PASSWORD=ADMIN_PASSWORD, REDIS_URL="", DATABASE_URL="", USERS_JSON="",
               RATE_LIMIT_PER_MINUTE="10000", PYTHONPATH=str(ROOT))
    # With mTLS required CORE publishes the CRL at startup, so the installation
    # CA exists before the process starts (as after bootstrap).
    sys.path.insert(0, str(ROOT))
    from core.node_registry import _create_installation_ca, create_node_csr
    (config / "pki").mkdir()
    _create_installation_ca(config / "pki/ca.key", config / "pki/ca.crt")
    core_port = _free_port()
    core_url = f"http://127.0.0.1:{core_port}"
    core_log = (tmp_path / "core.log").open("w", encoding="utf-8")
    core = subprocess.Popen([sys.executable, "-m", "uvicorn", "core.app:app", "--host", "127.0.0.1",
                             "--port", str(core_port)], cwd=ROOT, env=env,
                            stdout=core_log, stderr=subprocess.STDOUT)
    echo = _EchoUpstream()
    proxies = []
    try:
        def core_ready():
            assert core.poll() is None, (tmp_path / "core.log").read_text(encoding="utf-8")
            try:
                return httpx.get(core_url + "/api/health", timeout=3).status_code == 200
            except httpx.HTTPError:
                return False
        _wait(core_ready, "CORE")

        # Enroll two nodes through the stock registration flow; CORE issues the
        # node certificates from its own CA and publishes the CRL nginx loads.
        nodes = {}
        with httpx.Client(base_url=core_url, headers={"Authorization": AUTH}, timeout=30) as admin:
            for node in (NODE, OTHER):
                token = admin.post("/api/nodes/registration-tokens", json={"role": "voice"})
                assert token.status_code == 200, token.text
                directory = tmp_path / f"client-{node}"
                csr = create_node_csr(node, directory)
                issued = admin.post("/api/nodes/register", json={
                    "registration_token": token.json()["token"], "node_id": node,
                    "capabilities": ["speech_synthesis"], "hardware": {}, "version": "1.0.0", "csr": csr})
                assert issued.status_code == 200, issued.text
                body = issued.json()
                (directory / "node.crt").write_text(body["certificate"], encoding="utf-8")
                nodes[node] = {"token": body["worker_secret"], "serial": body["certificate_serial"],
                               "cert": (str(directory / "node.crt"), str(directory / "node.key"))}
        crl = config / "node-ca.crl"
        assert crl.is_file()

        tls = tmp_path / "tls"
        tls.mkdir()
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-keyout", str(tls / "vertep.key"),
                        "-out", str(tls / "vertep.crt")], check=True, capture_output=True)
        shutil.copy(config / "pki/ca.crt", tls / "node-ca.crt")
        core_proxy, proxy_url = _start_nginx(nginx, tmp_path / "nginx-core", tls, crl, core_url)
        proxies.append(core_proxy)
        echo_proxy, echo_url = _start_nginx(nginx, tmp_path / "nginx-echo", tls, crl, echo.url)
        proxies.append(echo_proxy)
        yield {"proxy": proxy_url, "echo_proxy": echo_url, "echo": echo, "nodes": nodes}
    finally:
        for process in (*proxies, core):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
        echo.close()
        core_log.close()


def _forged(node: dict, name: str) -> dict:
    """The right token plus the DN/Serial a certificate-less attacker would claim."""
    return {"x-vertep-token": node["token"], "x-vertep-client-dn": f"CN={name}",
            "x-vertep-client-serial": node["serial"], "x-vertep-node": name,
            "x-vertep-node-name": name}


# Worker routes served by ``location /`` (the general route of the audit finding).
GENERAL_ROUTES = [
    ("GET", "/api/tasks/publisher-readiness"),
    ("GET", "/api/tasks/missing/inputs/a.png"),
    ("GET", "/api/tasks/missing/character-references/a.png"),
    ("POST", "/api/workers/model-progress"),
    ("POST", "/api/watchdog/report"),
    ("POST", f"/api/nodes/{NODE}/self-test"),
]
# Worker routes of the mTLS regex location.
MTLS_ROUTES = [
    ("POST", "/api/workers/heartbeat"),
    ("POST", "/api/tasks/claim"),
    ("POST", "/api/tasks/result"),
    ("POST", "/api/tasks/claim/workflow"),
]


def _call(client: httpx.Client, method: str, path: str) -> httpx.Response:
    return client.request(method, path, json={} if method == "POST" else None)


def test_proxy_drops_forged_identity_on_every_route(stack):
    """What reaches the upstream: never the client's DN/Serial, only the verified peer."""
    echo, node, other = stack["echo"], stack["nodes"][NODE], stack["nodes"][OTHER]
    with httpx.Client(base_url=stack["echo_proxy"], headers=_forged(node, NODE),
                      verify=False, timeout=30) as attacker:
        for method, path in GENERAL_ROUTES:
            assert _call(attacker, method, path).status_code == 200
            assert echo.seen.pop(path) == {"dn": None, "serial": None}, path
        for method, path in MTLS_ROUTES:
            assert _call(attacker, method, path).status_code == 401
            assert path not in echo.seen
    with httpx.Client(base_url=stack["echo_proxy"], headers=_forged(node, NODE), cert=other["cert"],
                      verify=False, timeout=30) as impostor:
        for method, path in GENERAL_ROUTES + MTLS_ROUTES:
            assert _call(impostor, method, path).status_code == 200
            seen = echo.seen.pop(path)
            assert seen["dn"] and f"CN={OTHER}" in seen["dn"] and f"CN={NODE}" not in seen["dn"], path
            assert seen["serial"].upper().lstrip("0") == other["serial"].upper().lstrip("0"), path
    with httpx.Client(base_url=stack["echo_proxy"], verify=False, timeout=30) as browser:
        assert browser.get("/api/session", headers={"x-vertep-client-dn": "CN=x"}).status_code == 200
        assert echo.seen.pop("/api/session") == {"dn": None, "serial": None}


# Every worker route reaches CORE's own node identity check: the session
# middleware lets the Worker-only routes through to it (audit-1511 В1).
CORE_ROUTES = GENERAL_ROUTES + MTLS_ROUTES


def _core_calls(client: httpx.Client, name: str) -> dict[str, httpx.Response]:
    bodies = {"/api/workers/heartbeat": {"node_name": name, "role": "voice"},
              "/api/tasks/claim": {"node_name": name},
              "/api/tasks/claim/workflow": {"node_name": name},
              "/api/tasks/result": {"job_id": "none", "task_id": "none", "node_name": name,
                                    "success": False},
              "/api/workers/model-progress": {"node_name": name, "operation_id": "0" * 32,
                                              "status": "RUNNING"},
              "/api/watchdog/report": {},
              f"/api/nodes/{name}/self-test": {"status": "passed",
                                               "capabilities": ["speech_synthesis"]}}
    return {path: client.request(method, path, json=bodies.get(path))
            for method, path in CORE_ROUTES}


def test_forged_identity_without_certificate_is_refused_by_core(stack):
    node = stack["nodes"][NODE]
    with httpx.Client(base_url=stack["proxy"], headers=_forged(node, NODE),
                      verify=False, timeout=30) as attacker:
        responses = _core_calls(attacker, NODE)
    assert {path: r.status_code for path, r in responses.items()} == {path: 401 for path in responses}
    for path in ("/api/tasks/missing/inputs/a.png", "/api/tasks/missing/character-references/a.png",
                 "/api/tasks/publisher-readiness", "/api/workers/model-progress"):
        # Refused by the worker identity check itself, not by the proxy or middleware.
        assert responses[path].json()["code"] == "token_is_not_valid_for_this_worker"


def test_another_nodes_certificate_cannot_claim_this_node(stack):
    victim, other = stack["nodes"][NODE], stack["nodes"][OTHER]
    with httpx.Client(base_url=stack["proxy"], headers=_forged(victim, NODE), cert=other["cert"],
                      verify=False, timeout=30) as impostor:
        responses = _core_calls(impostor, NODE)
    assert {path: r.status_code for path, r in responses.items()} == {path: 401 for path in responses}


def test_certificate_and_token_path_is_accepted(stack):
    node = stack["nodes"][NODE]
    headers = {"x-vertep-token": node["token"], "x-vertep-node": NODE, "x-vertep-node-name": NODE,
               # Forged extras are overwritten with the verified peer, not trusted.
               "x-vertep-client-dn": "CN=someone-else", "x-vertep-client-serial": "00"}
    with httpx.Client(base_url=stack["proxy"], headers=headers, cert=node["cert"],
                      verify=False, timeout=30) as worker:
        responses = _core_calls(worker, NODE)
    statuses = {path: r.status_code for path, r in responses.items()}
    assert 401 not in statuses.values(), statuses
    assert statuses["/api/workers/heartbeat"] == 200, responses["/api/workers/heartbeat"].text
    assert statuses["/api/tasks/claim"] == 200
    assert statuses["/api/tasks/publisher-readiness"] == 200, responses["/api/tasks/publisher-readiness"].text
    assert statuses[f"/api/nodes/{NODE}/self-test"] == 200, responses[f"/api/nodes/{NODE}/self-test"].text
    assert statuses["/api/watchdog/report"] == 200, responses["/api/watchdog/report"].text


def test_web_and_setup_routes_do_not_regress(stack):
    with httpx.Client(base_url=stack["proxy"], verify=False, timeout=30) as browser:
        assert browser.get("/api/health").status_code == 200
        assert browser.post("/api/session", headers={"Authorization": AUTH}).status_code == 200
        assert browser.get("/api/setup/status").status_code != 401
        # The mTLS regex route still refuses a certificate-less caller at the proxy.
        assert browser.post("/api/workers/heartbeat", json={"node_name": NODE}).status_code == 401
