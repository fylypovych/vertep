"""#504 AC1/AC3/AC5/AC7: the panel is its own container behind the proxy.

Disposable Docker contour: the real proxy image (deploy/proxy.conf.template) and
the real web-ui image on one network in front of a real CORE process (uvicorn
``core.app:app`` with its own config, jobs and admin account). Inside the
network ``core`` is a plain TCP relay to that process, no logic of its own.
Proves routing (panel vs CORE paths, deep links, cache headers, build
identity), that stopping/removing the panel leaves the API reachable, that a
new panel build replaces the old one, and that a rollback keeps the real CORE
session and a real background task (a Worker lease taken before the rollback
completes after it) without restarting CORE.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin", "web-ui-contour-admin")
NODE_TOKEN = "web-ui-contour-node-token"
RELAY = (
    "import asyncio,sys\n"
    "target=sys.argv[1]; port=int(sys.argv[2])\n"
    "async def pipe(r,w):\n"
    "  try:\n"
    "    while (data:=await r.read(65536)): w.write(data); await w.drain()\n"
    "  finally: w.close()\n"
    "async def handle(r,w):\n"
    "  tr,tw=await asyncio.open_connection(target,port)\n"
    "  await asyncio.gather(pipe(r,tw),pipe(tr,w),return_exceptions=True)\n"
    "async def main():\n"
    "  server=await asyncio.start_server(handle,'0.0.0.0',8080)\n"
    "  async with server: await server.serve_forever()\n"
    "asyncio.run(main())\n")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        return probe.getsockname()[1]
pytestmark = [pytest.mark.skipif(shutil.which("docker") is None or shutil.which("openssl") is None,
                                 reason="needs docker and openssl"),
              # One contour, ordered steps (update, then rollback): one xdist worker.
              pytest.mark.xdist_group("web_ui_container")]


def run(*args, check=True, timeout=600):
    result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check:
        assert result.returncode == 0, f"{args}: {result.stderr[-2000:]}"
    return result


def build(tag, dockerfile, version):
    run("docker", "build", "-q", "-f", str(ROOT / dockerfile), "--build-arg", f"VERTEP_VERSION={version}",
        "-t", tag, str(ROOT), timeout=1800)


@pytest.fixture(scope="module")
def contour(tmp_path_factory):
    work = tmp_path_factory.mktemp("web-ui-contour")
    tls, revocation = work / "tls", work / "revocation"
    tls.mkdir()
    revocation.mkdir()
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=node-ca",
        "-keyout", str(tls / "node-ca.key"), "-out", str(tls / "node-ca.crt"))
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=vertep.test",
        "-keyout", str(tls / "vertep.key"), "-out", str(tls / "vertep.crt"))
    (work / "ca.cnf").write_text(
        "[ca]\ndefault_ca=ca\n[ca]\ndatabase=%s\ncrlnumber=%s\ndefault_md=sha256\ndefault_crl_days=2\n"
        % (work / "index.txt", work / "crlnumber"))
    (work / "index.txt").write_text("")
    (work / "crlnumber").write_text("01\n")
    run("openssl", "ca", "-config", str(work / "ca.cnf"), "-gencrl", "-keyfile", str(tls / "node-ca.key"),
        "-cert", str(tls / "node-ca.crt"), "-out", str(revocation / "node-ca.crl"))
    for item in list(tls.iterdir()) + list(revocation.iterdir()):
        item.chmod(0o644)
    suffix = uuid.uuid4().hex[:8]
    images = {"web-ui": f"vertep-test-web-ui:{suffix}", "web-ui-next": f"vertep-test-web-ui:{suffix}-next",
              "proxy": f"vertep-test-proxy:{suffix}"}
    build(images["web-ui"], "docker/web-ui/Dockerfile", "1.0.0-test")
    build(images["proxy"], "docker/proxy/Dockerfile", "1.0.0-test")
    network = f"vertep-test-{suffix}"
    names = {"core": f"core-{suffix}", "web-ui": f"web-ui-{suffix}", "proxy": f"proxy-{suffix}"}
    run("docker", "network", "create", network)
    # A real CORE process on the host; inside the network "core" is a TCP relay to it.
    from tests.media_harness import write_character
    core_root = work / "core"
    (core_root / "config").mkdir(parents=True)
    (core_root / "config" / "installation.json").write_text('{"completed_at": "2026-01-01"}', encoding="utf-8")
    write_character(core_root / "characters", "uichar")
    core_port = _free_port()
    core_env = {**os.environ, "PYTHONPATH": str(ROOT), "CONFIG_ROOT": str(core_root / "config"),
                "JOB_ROOT": str(core_root / "jobs"), "CHARACTERS_ROOT": str(core_root / "characters"),
                "UPDATE_STATE_DIR": str(core_root / "update"), "ADMIN_USER": ADMIN[0], "ADMIN_PASSWORD": ADMIN[1],
                "USERS_JSON": "{}", "NODE_API_TOKEN": NODE_TOKEN, "DATABASE_URL": "", "REDIS_URL": "",
                "SYSTEM_STATE_BACKEND": "file", "STATE_BACKEND": "file", "LOCAL_WORKER_FALLBACK": "false",
                "DEMO_MODE": "true", "RATE_LIMIT_PER_MINUTE": "10000", "OLLAMA_URL": "http://127.0.0.1:1"}
    core_log = (work / "core.log").open("w", encoding="utf-8")
    core = subprocess.Popen([sys.executable, "-m", "uvicorn", "core.app:app", "--host", "0.0.0.0",
                             "--port", str(core_port)], cwd=ROOT, env=core_env, stdout=core_log,
                            stderr=subprocess.STDOUT)
    import httpx
    direct = httpx.Client(base_url=f"http://127.0.0.1:{core_port}", auth=ADMIN, timeout=30,
                          headers={"x-vertep-token": NODE_TOKEN})
    for _ in range(240):
        if core.poll() is not None:
            break
        try:
            if direct.get("/api/status").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    if core.poll() is not None or direct.get("/api/status").status_code != 200:
        core.kill()
        raise AssertionError("CORE did not start: " + (work / "core.log").read_text()[-3000:])
    try:
        run("docker", "run", "-d", "--name", names["core"], "--network", network, "--network-alias", "core",
            "--add-host", "host.docker.internal:host-gateway",
            "python:3.12-slim", "python", "-c", RELAY, "host.docker.internal", str(core_port))

        def start_web_ui(image):
            run("docker", "run", "-d", "--name", names["web-ui"], "--network", network, "--network-alias", "web-ui",
                "--read-only", "--tmpfs", "/var/cache/nginx", "--tmpfs", "/var/run", "--tmpfs", "/tmp", image)

        start_web_ui(images["web-ui"])
        run("docker", "run", "-d", "--name", names["proxy"], "--network", network, "-p", "127.0.0.1::8443",
            "-e", "WEB_DOMAIN=vertep.test", "-v", f"{tls}:/etc/vertep/tls:ro", "-v", f"{revocation}:/etc/vertep/revocation:ro",
            "--entrypoint", "/usr/local/bin/vertep-proxy-entrypoint", images["proxy"])
        port = run("docker", "port", names["proxy"], "8443").stdout.split(":")[-1].strip()
        # Ready only when the proxy reaches the real CORE through the relay (no 502 race).
        for _ in range(120):
            try:
                if httpx.get(f"https://127.0.0.1:{port}/api/session", verify=False, timeout=5).status_code == 401:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise AssertionError("the proxy never reached CORE: "
                                 + run("docker", "logs", names["core"], check=False).stderr[-2000:])
        yield {"base": f"https://127.0.0.1:{port}", "names": names, "images": images, "start_web_ui": start_web_ui,
               "core": core, "direct": direct}
    finally:
        core.terminate()
        try:
            core.wait(timeout=20)
        except subprocess.TimeoutExpired:
            core.kill()
        direct.close()
        core_log.close()
        for name in names.values():
            run("docker", "rm", "-f", name, check=False)
        run("docker", "network", "rm", network, check=False)
        for image in images.values():
            run("docker", "rmi", "-f", image, check=False)


def get(contour, path):
    import httpx
    for _ in range(60):
        try:
            return httpx.get(contour["base"] + path, verify=False, timeout=5)
        except httpx.HTTPError:
            time.sleep(0.5)
    raise AssertionError(f"proxy did not answer {path}")


def test_panel_and_core_paths_are_routed_to_their_own_containers(contour):
    shell = get(contour, "/")
    assert shell.status_code == 200 and "<app-root" in shell.text
    assert "no-cache" in shell.headers.get("cache-control", "")
    deep = get(contour, "/jobs/2026-000123")  # an Angular deep link, not a CORE path
    assert deep.status_code == 200 and "<app-root" in deep.text
    # Answered by the real CORE behind the proxy (its own auth), never by the panel shell.
    for path in ("/api/session", "/api/status"):
        answer = get(contour, path)
        assert answer.status_code == 401 and "<app-root" not in answer.text, path
    media = get(contour, "/jobs/j1/files/final/video.mp4")  # a CORE path, never the panel shell
    assert media.status_code == 404 and "<app-root" not in media.text  # CORE: no such job
    identity = get(contour, "/ui-version.json").json()
    assert identity["version"] == "1.0.0-test" and identity["api_major"] == 1
    bundle = next(part.split('"')[0] for part in shell.text.split('src="')[1:] if part.split('"')[0].endswith(".js"))
    assert "immutable" in get(contour, "/" + bundle).headers.get("cache-control", "")


def test_stopping_the_panel_leaves_core_and_the_proxy_working_and_a_new_build_replaces_it(contour):
    names = contour["names"]
    run("docker", "rm", "-f", names["web-ui"])
    assert get(contour, "/api/session").status_code == 401  # CORE keeps answering without the panel
    assert get(contour, "/").status_code in {502, 504}
    assert run("docker", "inspect", "-f", "{{.State.Running}}", names["proxy"]).stdout.strip() == "true"
    build(contour["images"]["web-ui-next"], "docker/web-ui/Dockerfile", "1.0.1-test")
    contour["start_web_ui"](contour["images"]["web-ui-next"])  # independent panel update
    for _ in range(40):
        if get(contour, "/ui-version.json").status_code == 200:
            break
        time.sleep(0.5)
    assert get(contour, "/ui-version.json").json()["version"] == "1.0.1-test"
    assert contour["core"].poll() is None  # the CORE process was never restarted
    assert get(contour, "/api/session").status_code == 401


def test_rolling_the_panel_back_keeps_the_session_core_work_and_reports_the_old_build(contour):
    """AC3/AC5/AC7: the newer panel (left by the update test) is rolled back to the
    previous image while a real CORE session stays signed in; a Worker lease taken on
    a real CORE task before the rollback completes after it; CORE is not restarted;
    the old build identity, image digest and fresh shell are served."""
    from tests.test_media_browser_e2e import sync_playwright, expect
    from tests import media_harness as harness
    names, images, direct = contour["names"], contour["images"], contour["direct"]
    assert get(contour, "/ui-version.json").json()["version"] == "1.0.1-test"
    core_pid = contour["core"].pid
    created = direct.post("/api/jobs", json={"topic": "Rollback while working", "character_id": "uichar"})
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    direct.post("/api/workers/heartbeat", json={"node_name": "ui-text", "role": "text", "vram_mb": 0,
                                                "capabilities": ["text_generation"], "supported_tasks": ["text"]})
    task = harness.claim_task(direct, job_id, "ui-text", task="script")  # background work in flight
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            login = "Basic " + base64.b64encode(f"{ADMIN[0]}:{ADMIN[1]}".encode()).decode()
            assert page.request.post(contour["base"] + "/api/session", headers={"Authorization": login}).status == 200
            page.goto(contour["base"] + "/jobs")
            expect(page.get_by_test_id("panel-version")).to_contain_text("1.0.1-test", timeout=20000)
            # Rollback: the previous panel image replaces the current one.
            run("docker", "rm", "-f", names["web-ui"])
            contour["start_web_ui"](images["web-ui"])
            for _ in range(40):
                response = get(contour, "/ui-version.json")
                if response.status_code == 200 and response.json()["version"] == "1.0.0-test":
                    break
                time.sleep(0.5)
            page.reload()
            expect(page.get_by_test_id("panel-version")).to_contain_text("1.0.0-test", timeout=20000)
            expect(page.get_by_role("heading", name="Вхід")).to_have_count(0)  # still signed in
            assert page.request.get(contour["base"] + "/api/session").json()["authenticated"] is True
            # The lease taken before the rollback completes on the same CORE afterwards.
            board = {"title": "Rollback", "description": "опис", "hashtags": ["#test"], "scenes": [
                {"prompt": "Кадр", "voiceover": "Текст", "duration": 3.0}]}
            artifact = {"kind": "script", "filename": "script.json",
                        "data_base64": base64.b64encode(json.dumps(board).encode()).decode()}
            result = direct.post("/api/tasks/result", json={"job_id": job_id, "task_id": task["task_id"],
                                                            "node_name": "ui-text", "success": True,
                                                            "artifacts": [artifact]})
            assert result.status_code == 200, result.text
            for _ in range(60):
                job = page.request.get(contour["base"] + f"/api/jobs/{job_id}").json()
                if job["status"] == "SCRIPT_PENDING_APPROVAL":
                    break
                time.sleep(0.5)
            assert job["status"] == "SCRIPT_PENDING_APPROVAL" and job["script"]["title"] == "Rollback"
        finally:
            browser.close()
    assert contour["core"].poll() is None and contour["core"].pid == core_pid  # CORE never restarted
    running_image = run("docker", "inspect", "-f", "{{.Image}}", names["web-ui"]).stdout.strip()
    assert running_image == run("docker", "image", "inspect", "-f", "{{.Id}}", images["web-ui"]).stdout.strip()
    identity = get(contour, "/ui-version.json")
    assert identity.json()["version"] == "1.0.0-test" and "commit_sha" in identity.json()
    assert "no-cache" in identity.headers.get("cache-control", "")
    assert "no-cache" in get(contour, "/").headers.get("cache-control", "")
