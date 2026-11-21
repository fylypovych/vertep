"""Stage 2 of the separately released Publisher (#510/#511, owner decision 21.11.2026):
its own container that only uploads, with the task's credentials handed over by the Worker."""

import base64
import importlib.util
import json
import shutil
import subprocess
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "modules" / "publisher" / "app"


@pytest.fixture
def app(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("publisher_app", APP / "publisher_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "TOKEN", "module-token")
    monkeypatch.setattr(module, "INPUTS", tmp_path / "inputs")
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield module, f"http://127.0.0.1:{server.server_address[1]}", tmp_path / "inputs"
    server.shutdown()


def header(channel="youtube", credentials=None, metadata=None):
    return base64.b64encode(json.dumps({"channel": channel, "metadata": metadata or {"title": "t"},
                                        "credentials": credentials or {}}).encode()).decode()


class FakePublisher:
    credential_env = "YOUTUBE_ACCESS_TOKEN"

    def __init__(self):
        self.calls = []

    def configured(self):
        from publishers.task_secrets import current_task_secrets
        return current_task_secrets().get("youtube_access_token") is not None

    def publish(self, path, metadata):
        from publishers.task_secrets import current_task_secrets
        secrets = current_task_secrets()
        self.calls.append((Path(path).read_bytes(), metadata, secrets.get("youtube_access_token")))
        secrets.save({"youtube_access_token": "refreshed"}, {"YOUTUBE_ACCESS_TOKEN": {"expires_at": 1.0}})
        return {"channel": "youtube", "status": "PUBLISHED", "id": "remote-1"}


def test_the_container_uploads_with_task_credentials_and_returns_refreshed_tokens(app, monkeypatch):
    module, url, inputs = app
    fake = FakePublisher()
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", {"youtube": fake})
    response = httpx.post(f"{url}/api/v1/publish", content=b"approved-video",
                          headers={"x-vertep-module-token": "module-token",
                                   "x-vertep-publish": header(credentials={"secrets": {"YOUTUBE_ACCESS_TOKEN": "t1"}})})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["status"] == "PUBLISHED"
    assert fake.calls == [(b"approved-video", {"title": "t"}, "t1")]
    assert body["credential_updates"] == {"secrets": {"youtube_access_token": "refreshed"},
                                          "oauth": {"YOUTUBE_ACCESS_TOKEN": {"expires_at": 1.0}}}
    assert list(inputs.iterdir()) == []  # the video copy never outlives the request


def test_only_the_module_token_may_publish_and_bad_requests_are_refused(app, monkeypatch):
    module, url, _ = app
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", {"youtube": FakePublisher()})
    assert httpx.post(f"{url}/api/v1/publish", content=b"v",
                      headers={"x-vertep-publish": header()}).status_code == 403
    assert httpx.post(f"{url}/api/v1/publish", content=b"v",
                      headers={"x-vertep-module-token": "wrong", "x-vertep-publish": header()}).status_code == 403
    assert httpx.post(f"{url}/api/v1/publish", content=b"v",
                      headers={"x-vertep-module-token": "module-token", "x-vertep-publish": "%%%"}).status_code == 400
    assert httpx.post(f"{url}/api/v1/publish", content=b"",
                      headers={"x-vertep-module-token": "module-token", "x-vertep-publish": header()}).status_code == 400
    assert httpx.get(f"{url}/health").json()["status"] == "ok"


def test_an_unconfigured_channel_is_a_not_configured_result(app, monkeypatch):
    module, url, _ = app
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", {"youtube": FakePublisher()})
    response = httpx.post(f"{url}/api/v1/publish", content=b"v",
                          headers={"x-vertep-module-token": "module-token", "x-vertep-publish": header()})
    assert response.json()["result"]["status"] == "NOT_CONFIGURED"


def test_the_manifest_is_a_signed_package_ready_application_module():
    from core.module.manifest import ModuleManifest
    manifest = ModuleManifest.from_dict(yaml.safe_load((APP / "manifest.yaml").read_text(encoding="utf-8")))
    assert manifest.kind == "application" and manifest.module_id == "publisher"
    assert manifest.publisher == "vertep" and manifest.provenance["repository"] == "github.com/fylypovych/vertep"
    assert manifest.core_compatibility and manifest.api["namespace"] == "publisher"
    assert manifest.container["port"] == 8080
    # Until stage 3 the bundled role manifest stays the one the Worker and CORE use.
    bundled = yaml.safe_load((ROOT / "modules" / "publisher" / "manifest.yaml").read_text(encoding="utf-8"))
    assert bundled.get("role") == "publisher" and "kind" not in bundled


def _docker():
    return shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker(), reason="needs a Docker daemon")
def test_the_image_runs_hardened_like_on_a_node_and_publishes(tmp_path):
    """The real image under the module supervisor's restrictions (read-only, no capabilities,
    unprivileged user, own data volume): the volume is writable and a mock upload succeeds."""
    tag, name = f"vertep-publisher-test:{uuid.uuid4().hex[:8]}", f"vertep-publisher-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(APP / "Dockerfile"), str(ROOT)],
                   check=True, capture_output=True, timeout=600)
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--read-only", "--cap-drop=ALL",
                        "--security-opt=no-new-privileges", "--user=65532:65532",
                        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m", f"--mount=type=volume,src={name}-data,dst=/data",
                        "-e", "VERTEP_MODULE_TOKEN=t", "-e", "DEMO_MODE=true", "-e", "PUBLISHER_MOCK=true",
                        "-p", "127.0.0.1::8080", tag], check=True, capture_output=True)
        port = subprocess.run(["docker", "port", name, "8080"], capture_output=True, text=True,
                              check=True).stdout.split(":")[-1].strip()
        import time
        for _ in range(40):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        response = httpx.post(f"http://127.0.0.1:{port}/api/v1/publish", content=b"video",
                              headers={"x-vertep-module-token": "t", "x-vertep-publish": header()}, timeout=30)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["status"] == "PUBLISHED"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", f"{name}-data"], capture_output=True)
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)
