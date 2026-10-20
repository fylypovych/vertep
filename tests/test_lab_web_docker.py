"""Disposable HTTP/Compose proof for the Lab Web image, not UI acceptance."""

import json
import os
from pathlib import Path
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
        timeout=600, **kwargs,
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
    with httpx.Client(trust_env=False, timeout=2) as client:
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
    logs = run("logs", name, check=False).stdout.strip()
    pytest.fail(
        f"Container {name} did not publish {container_port} in time; "
        f"state={inspection.get('State')}; logs={logs}"
    )


def readable_asset_dir():
    """Create an asset directory the container user can traverse and read.

    pytest creates its temporary directories with mode 0700, which the
    unprivileged container user cannot enter, so a plain system temporary
    directory with explicit permissions is used instead.
    """
    root = Path(tempfile.mkdtemp(prefix="vertep-lab-web-"))
    os.chmod(root, 0o755)
    assets = root / "browser"
    assets.mkdir(mode=0o755)
    return root, assets


def test_web_compose_requires_real_dist_and_is_isolated(docker, tmp_path):
    compose = ROOT / "deploy/lab/docker-compose.web.yml"
    env = {**os.environ, "LAB_WEB_DIST": str(tmp_path)}
    config = json.loads(run(
        "compose", "-f", str(compose), "config", "--format", "json", env=env,
    ).stdout)
    services = config["services"]
    assert set(services) == {"lab-web", "lab-control"}
    web = services["lab-web"]
    assert Path(web["build"]["context"]).resolve() == ROOT
    assert web["ports"][0]["host_ip"] == "127.0.0.1"
    assert web["read_only"] is True
    assert web["volumes"][0]["read_only"] is True
    assert not services["lab-control"].get("ports")
    assert services["lab-control"]["command"][0] == "uvicorn"
    assert all("docker.sock" not in str(s.get("volumes", [])) for s in services.values())
    assert all(not s.get("privileged") for s in services.values())
    # A missing asset directory must not silently create an empty host folder.
    env.pop("LAB_WEB_DIST")
    result = run("compose", "-f", str(compose), "config", env=env, check=False)
    assert result.returncode != 0
    assert "LAB_WEB_DIST" in result.stderr


def test_web_image_http_readiness_spa_and_authenticated_api_proxy(docker):
    suffix = uuid.uuid4().hex[:12]
    network = f"vertep-lab-web-test-{suffix}"
    api = f"{network}-api"
    web = f"{network}-web"
    image = f"vertep-lab-web-test:{suffix}"
    asset_root, assets = readable_asset_dir()
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
            "-p", "127.0.0.1::8080", "-v", f"{assets}:/srv/lab-web:ro", image)
        inspection, port = wait_for_published_port(web, "8080/tcp")
        assert inspection["Config"]["User"] == "nginx"
        assert inspection["State"]["Running"] is True
        origin = f"http://127.0.0.1:{port}"
        wait_for(origin + "/healthz", lambda r: r.status_code == 200)
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            assert client.get("/readyz").status_code == 503
            assert client.get("/review/example").status_code == 503
            index = "<!doctype html><html lang='uk'><body>Тест збірки</body></html>"
            (assets / "index.html").write_text(index, encoding="utf-8")
            (assets / "app.js").write_text("window.labFixture = true;", encoding="utf-8")
            assert client.get("/readyz").status_code == 200
            page = client.get("/review/example")
            assert page.status_code == 200 and page.text == index
            assert page.headers["cache-control"] == "no-store"
            assert client.get("/app.js").status_code == 200
            assert client.get("/missing.js").status_code == 404
            assert client.get("/api/jobs").status_code == 404
            response = wait_for(origin + "/api/lab/status", lambda r: r.status_code == 200)
            assert response.json()["path"] == "/api/lab/status"
            response = client.get("/api/lab/status?case=proxy", headers={
                "Authorization": "Bearer synthetic-test-token", "Cookie": "lab=test",
            })
            assert response.json() == {"path": "/api/lab/status?case=proxy",
                                       "authorization": "Bearer synthetic-test-token",
                                       "cookie": "lab=test"}
            assert client.get("/api/lab/conflict").status_code == 409
        result = run("exec", web, "sh", "-c", "echo forbidden > /srv/lab-web/write", check=False)
        assert result.returncode != 0
    finally:
        # Only uniquely named, disposable artifacts created by this test.
        run("rm", "-f", web, api, check=False)
        run("network", "rm", network, check=False)
        run("image", "rm", image, check=False)
        shutil.rmtree(asset_root, ignore_errors=True)
