"""Disposable HTTP/Compose proof for the Lab Web image, not UI acceptance.

The image bakes the real Angular laboratory build (Issue #251, U1), so this
test proves packaging, read-only isolation, SPA fallback and the authenticated
API proxy.  Actual browser/CLI acceptance against the live backend lives in
``tests/test_lab_web_e2e.py``; neither test replaces the other.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]


def run(*args, check=True, **kwargs):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True,
        timeout=1200, **kwargs,
    )


@pytest.fixture(scope="module")
def docker():
    available = shutil.which("docker") is not None
    if available:
        available = run("info", check=False).returncode == 0
    if not available:
        if os.getenv("VERTEP_REQUIRE_LAB_WEB_DOCKER_TESTS") == "1":
            pytest.fail("Lab Web Docker proof is required but Docker is unavailable")
        pytest.skip("Docker daemon is unavailable; this is not image runtime proof")


def wait_for(url, predicate):
    deadline = time.monotonic() + 30
    with httpx.Client(trust_env=False, timeout=2, verify=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                if predicate(response):
                    return response
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    pytest.fail(f"Timed out waiting for {url}")


def wait_for_published_port(name, container_port, timeout=60):
    """Return the host port of a container published with an ephemeral port.

    The binding appears in the inspection only after the container has started,
    so the lookup has to wait instead of reading a single snapshot.
    """
    deadline = time.monotonic() + timeout
    inspection = {}
    while time.monotonic() < deadline:
        inspection = json.loads(run("inspect", name).stdout)[0]
        bindings = (inspection.get("NetworkSettings", {}).get("Ports") or {}).get(
            container_port)
        if bindings:
            return inspection, bindings[0]["HostPort"]
        time.sleep(0.2)
    logs = subprocess.run(
        ["docker", "logs", name], capture_output=True, text=True, timeout=60,
        stderr=subprocess.STDOUT,
    ).stdout.strip()
    pytest.fail(
        f"Container {name} did not publish {container_port} in time; "
        f"state={inspection.get('State')}; logs={logs}"
    )


def test_web_compose_is_self_contained_and_isolated(docker):
    compose = ROOT / "deploy/lab/docker-compose.web.yml"
    build = ROOT / "deploy/lab/docker-compose.web.build.yml"  # development overlay (#491 L5)
    config = json.loads(run(
        "compose", "-f", str(compose), "-f", str(build), "config", "--format", "json",
    ).stdout)
    services = config["services"]
    assert set(services) == {"lab-web", "lab-control", "lab-executor"}
    executor = services["lab-executor"]
    assert not executor.get("ports")
    assert executor["read_only"] is True
    assert executor["command"][:2] == ["uvicorn", "lab.role_service:create_app"]
    web = services["lab-web"]
    assert Path(web["build"]["context"]).resolve() == ROOT
    assert web["build"]["dockerfile"] == "deploy/lab/Dockerfile.web"
    # #491 L2/L8: only the HTTPS panel, on the VM address.
    assert [(p["host_ip"], p["target"]) for p in web["ports"]] == [("0.0.0.0", 8443)]
    assert web["read_only"] is True
    # The panel build is baked into the image; the only host mount is the TLS pair,
    # read-only, so nothing can substitute production assets (Issue #251, U1).
    assert [(v["target"], v.get("read_only")) for v in web["volumes"]] == [("/run/lab-tls", True)]
    assert not services["lab-control"].get("ports")
    assert services["lab-control"]["command"][0] == "uvicorn"
    assert all("docker.sock" not in str(s.get("volumes", [])) for s in services.values())
    assert all(not s.get("privileged") for s in services.values())


def test_web_image_serves_baked_angular_and_authenticated_api_proxy(docker):
    suffix = uuid.uuid4().hex[:12]
    network = f"vertep-lab-web-test-{suffix}"
    api = f"{network}-api"
    web = f"{network}-web"
    image = f"vertep-lab-web-test:{suffix}"
    server = """
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({'path': self.path,
                           'authorization': self.headers.get('Authorization'),
                           'cookie': self.headers.get('Cookie')}).encode()
        self.send_response(409 if self.path.endswith('/conflict') else 200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""
    tls = Path(tempfile.mkdtemp(prefix="lab-tls-"))
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-days", "1", "-subj", "/CN=localhost", "-keyout", str(tls / "lab.key"),
                    "-out", str(tls / "lab.crt")], check=True, capture_output=True)
    os.chmod(tls, 0o755)
    os.chmod(tls / "lab.key", 0o644)  # disposable test key; nginx (uid 101) must read it
    try:
        run("build", "-f", str(ROOT / "deploy/lab/Dockerfile.web"),
            "-t", image, str(ROOT))
        run("network", "create", network)
        run("run", "-d", "--name", api, "--network", network,
            "--network-alias", "lab-control", "python:3.12-alpine",
            "python", "-u", "-c", server)
        run("run", "-d", "--name", web, "--network", network,
            "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "-v", f"{tls}:/run/lab-tls:ro", "-p", "127.0.0.1::8443", image)
        inspection, port = wait_for_published_port(web, "8443/tcp")
        assert inspection["Config"]["User"] == "nginx"
        assert inspection["State"]["Running"] is True
        origin = f"https://127.0.0.1:{port}"
        wait_for(origin + "/healthz", lambda r: r.status_code == 200)
        with httpx.Client(base_url=origin, trust_env=False, timeout=10, verify=False) as client:
            assert client.get("/readyz").status_code == 200
            index = client.get("/")
            assert index.status_code == 200
            assert "<app-root>" in index.text and "Vertep Lab" in index.text
            assert index.headers["cache-control"] == "no-store"
            # Angular deep link falls back to the SPA, not to a 404.
            deep = client.get("/approvals")
            assert deep.status_code == 200 and deep.text == index.text
            script = re.search(r'src="([^"]+\.js)"', index.text)
            assert script, "Angular build did not reference its main bundle"
            assert client.get(script.group(1)).status_code == 200
            assert client.get("/missing.js").status_code == 404
            assert client.get("/api/jobs").status_code == 404
            response = wait_for(origin + "/api/lab/status",
                                lambda r: r.status_code == 200)
            assert response.json()["path"] == "/api/lab/status"
            response = client.get("/api/lab/status?case=proxy", headers={
                "Authorization": "Bearer synthetic-test-token", "Cookie": "lab=test",
            })
            assert response.json() == {"path": "/api/lab/status?case=proxy",
                                       "authorization": "Bearer synthetic-test-token",
                                       "cookie": "lab=test"}
            assert client.get("/api/lab/conflict").status_code == 409
        result = run("exec", web, "sh", "-c", "echo forbidden > /srv/lab-web/write",
                     check=False)
        assert result.returncode != 0
    finally:
        # Only uniquely named, disposable artifacts created by this test.
        run("rm", "-f", web, api, check=False)
        run("network", "rm", network, check=False)
        run("image", "rm", image, check=False)
        shutil.rmtree(tls, ignore_errors=True)
