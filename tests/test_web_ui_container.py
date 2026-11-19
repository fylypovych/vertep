"""#504 AC1/AC3/AC5/AC7: the panel is its own container behind the proxy.

Disposable Docker contour: the real proxy image (deploy/proxy.conf.template) and
the real web-ui image on one network with a stand-in CORE HTTP service. Proves
routing (panel vs CORE paths, deep links, cache headers, build identity), that
stopping/removing the panel leaves the API reachable, and that a new panel
build replaces the old one without touching CORE.
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None or shutil.which("openssl") is None,
                                reason="needs docker and openssl")


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
    # Stand-in CORE: a session cookie owned by CORE (not by the panel), a status with
    # the API major, and a background task whose counter keeps advancing.
    core_script = (
        "import http.server,json,threading,time\n"
        "ticks=[0]\n"
        "def work():\n"
        "  while True:\n"
        "    time.sleep(0.2); ticks[0]+=1\n"
        "threading.Thread(target=work,daemon=True).start()\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "  def log_message(s,*a): pass\n"
        "  def reply(s,b,cookie=None):\n"
        "    d=json.dumps(b).encode();s.send_response(200)\n"
        "    s.send_header('content-type','application/json')\n"
        "    if cookie: s.send_header('set-cookie',cookie)\n"
        "    s.end_headers();s.wfile.write(d)\n"
        "  def authed(s): return 'vertep_session=core-session' in (s.headers.get('cookie') or '')\n"
        "  def do_POST(s):\n"
        "    if s.path=='/api/session': return s.reply({'authenticated':True,'user':'admin','login':'admin','role':'admin','brands':None},"
        "'vertep_session=core-session; Path=/; HttpOnly; SameSite=Strict')\n"
        "    s.reply({})\n"
        "  def do_GET(s):\n"
        "    if s.path=='/api/session': return s.reply({'authenticated':True,'user':'admin','login':'admin','role':'admin','brands':None} if s.authed() else {'authenticated':False})\n"
        "    if s.path=='/api/status': return s.reply({'core':'OK','version':'0.0.4.12','api_major':1,'system':{'state':'NORMAL'}})\n"
        "    if s.path=='/api/background': return s.reply({'ticks':ticks[0]})\n"
        "    if s.path.startswith('/api/jobs') or s.path.startswith('/api/workers') or s.path.startswith('/api/modules'): return s.reply([])\n"
        "    s.reply({'core':True,'path':s.path})\n"
        "http.server.ThreadingHTTPServer(('',8080),H).serve_forever()\n")
    try:
        run("docker", "run", "-d", "--name", names["core"], "--network", network, "--network-alias", "core",
            "python:3.12-slim", "python", "-c", core_script)

        def start_web_ui(image):
            run("docker", "run", "-d", "--name", names["web-ui"], "--network", network, "--network-alias", "web-ui",
                "--read-only", "--tmpfs", "/var/cache/nginx", "--tmpfs", "/var/run", "--tmpfs", "/tmp", image)

        start_web_ui(images["web-ui"])
        run("docker", "run", "-d", "--name", names["proxy"], "--network", network, "-p", "127.0.0.1::8443",
            "-e", "WEB_DOMAIN=vertep.test", "-v", f"{tls}:/etc/vertep/tls:ro", "-v", f"{revocation}:/etc/vertep/revocation:ro",
            "--entrypoint", "/usr/local/bin/vertep-proxy-entrypoint", images["proxy"])
        port = run("docker", "port", names["proxy"], "8443").stdout.split(":")[-1].strip()
        yield {"base": f"https://127.0.0.1:{port}", "names": names, "images": images, "start_web_ui": start_web_ui}
    finally:
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
    assert get(contour, "/api/health").json() == {"core": True, "path": "/api/health"}
    assert get(contour, "/jobs/j1/files/final/video.mp4").json()["core"] is True
    assert get(contour, "/status").json()["core"] is True
    identity = get(contour, "/ui-version.json").json()
    assert identity["version"] == "1.0.0-test" and identity["api_major"] == 1
    bundle = next(part.split('"')[0] for part in shell.text.split('src="')[1:] if part.split('"')[0].endswith(".js"))
    assert "immutable" in get(contour, "/" + bundle).headers.get("cache-control", "")


def test_stopping_the_panel_leaves_core_and_the_proxy_working_and_a_new_build_replaces_it(contour):
    names = contour["names"]
    run("docker", "rm", "-f", names["web-ui"])
    assert get(contour, "/api/health").json()["core"] is True  # API keeps working without the panel
    assert get(contour, "/").status_code in {502, 504}
    assert run("docker", "inspect", "-f", "{{.State.Running}}", names["proxy"]).stdout.strip() == "true"
    build(contour["images"]["web-ui-next"], "docker/web-ui/Dockerfile", "1.0.1-test")
    contour["start_web_ui"](contour["images"]["web-ui-next"])  # independent panel update
    for _ in range(40):
        if get(contour, "/ui-version.json").status_code == 200:
            break
        time.sleep(0.5)
    assert get(contour, "/ui-version.json").json()["version"] == "1.0.1-test"
    core_started = run("docker", "inspect", "-f", "{{.State.StartedAt}}", names["core"]).stdout
    assert core_started == run("docker", "inspect", "-f", "{{.State.StartedAt}}", names["core"]).stdout
    assert get(contour, "/api/health").json()["core"] is True


def test_rolling_the_panel_back_keeps_the_session_core_work_and_reports_the_old_build(contour):
    """AC3/AC5/AC7: the newer panel (left by the update test) is rolled back to the
    previous image while a browser session stays signed in; CORE and its background
    work run on; the old build identity, image digest and fresh shell are served."""
    from tests.test_media_browser_e2e import sync_playwright, expect
    names, images = contour["names"], contour["images"]
    assert get(contour, "/ui-version.json").json()["version"] == "1.0.1-test"
    core_started = run("docker", "inspect", "-f", "{{.State.StartedAt}}", names["core"]).stdout
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            assert page.request.post(contour["base"] + "/api/session").status == 200
            page.goto(contour["base"] + "/jobs")
            expect(page.get_by_test_id("panel-version")).to_contain_text("1.0.1-test", timeout=20000)
            ticks_before = page.request.get(contour["base"] + "/api/background").json()["ticks"]
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
            assert page.request.get(contour["base"] + "/api/background").json()["ticks"] > ticks_before
        finally:
            browser.close()
    assert run("docker", "inspect", "-f", "{{.State.StartedAt}}", names["core"]).stdout == core_started
    running_image = run("docker", "inspect", "-f", "{{.Image}}", names["web-ui"]).stdout.strip()
    assert running_image == run("docker", "image", "inspect", "-f", "{{.Id}}", images["web-ui"]).stdout.strip()
    identity = get(contour, "/ui-version.json")
    assert identity.json()["version"] == "1.0.0-test" and "commit_sha" in identity.json()
    assert "no-cache" in identity.headers.get("cache-control", "")
    assert "no-cache" in get(contour, "/").headers.get("cache-control", "")
