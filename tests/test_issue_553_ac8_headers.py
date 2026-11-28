"""Issue #553 AC8: Web UI HTML security headers and HTTPS HSTS.

Verifies:
1. Static nginx configuration invariants across ``deploy/proxy.conf``,
   ``deploy/proxy.conf.template`` and ``docker/web-ui/nginx.conf``:
   - HTML Web UI responses carry CSP, ``X-Frame-Options``,
     ``X-Content-Type-Options`` and ``Referrer-Policy`` (including every
     location that sets ``add_header Cache-Control`` so nginx does not drop
     server-level headers).
   - Every HTTPS response from the proxy carries
     ``Strict-Transport-Security`` with ``always`` (including ``location /``,
     ``/api/*`` and proxy-level 401 mTLS denials).
   - CORE ``/api/*`` locations do not duplicate or clobber CORE security
     headers (such as module UI ``sandbox`` CSP).
   - The Web UI CSP allows Angular Web UI, module UI iframes and HTTPS LAB
     iframes without external CDNs.
2. Live ``nginx:1.27-alpine`` syntax checks (``nginx -t``) and real HTTPS /
   HTTP response header checks (no duplicate headers, HSTS everywhere on
   HTTPS, HTML & SPA deep links secured, CORE and module UI headers intact).
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY_CONFIGS = (ROOT / "deploy/proxy.conf", ROOT / "deploy/proxy.conf.template")
WEB_UI_CONFIG = ROOT / "docker/web-ui/nginx.conf"
NGINX_IMAGE = "nginx:1.27-alpine"

ANGULAR_ONLOAD_HASH = (
    "sha256-"
    + base64.b64encode(hashlib.sha256(b"this.media='all'").digest()).decode("ascii")
)
EXPECTED_HSTS = "max-age=31536000; includeSubDomains"
EXPECTED_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    f"script-src-attr 'unsafe-hashes' '{ANGULAR_ONLOAD_HASH}'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' blob:; "
    "connect-src 'self' ws: wss:; "
    "frame-src 'self' https:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'"
)


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _extract_blocks(text: str, keyword: str) -> list[tuple[str, str]]:
    """Return ``(header, body)`` for each top-level ``<keyword> ... { ... }`` block."""
    cleaned = _strip_comments(text)
    blocks: list[tuple[str, str]] = []
    pattern = re.compile(rf"\b{re.escape(keyword)}\b([^{{;]*)\{{")
    pos = 0
    while True:
        match = pattern.search(cleaned, pos)
        if not match:
            break
        header = match.group(1).strip()
        start = match.end()
        depth = 1
        idx = start
        while idx < len(cleaned) and depth > 0:
            char = cleaned[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            idx += 1
        assert depth == 0, f"Unbalanced braces in {keyword} {header}"
        blocks.append((header, cleaned[start : idx - 1]))
        pos = idx
    return blocks


def _direct_directives(block_body: str) -> str:
    """Return text of ``block_body`` excluding nested ``{ ... }`` blocks."""
    out: list[str] = []
    depth = 0
    for char in block_body:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _parse_add_headers(body: str) -> dict[str, tuple[str, bool]]:
    """Parse ``add_header Name value [always];`` lines directly in ``body``."""
    direct = _direct_directives(body)
    found: dict[str, tuple[str, bool]] = {}
    pattern = re.compile(
        r'add_header\s+([A-Za-z0-9-]+)\s+(?:"([^"]+)"|(\S+))(\s+always)?\s*;'
    )
    for match in pattern.finditer(direct):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        always = bool(match.group(4))
        found[name] = (value, always)
    return found


def _parse_hide_headers(body: str) -> set[str]:
    direct = _direct_directives(body)
    return set(re.findall(r"proxy_hide_header\s+([A-Za-z0-9-]+)\s*;", direct))


def _parse_csp(csp: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for raw in csp.split(";"):
        part = raw.strip()
        if not part:
            continue
        tokens = part.split()
        directives[tokens[0].lower()] = tokens[1:]
    return directives


# ── Static config invariants ────────────────────────────────────────────────


def test_proxy_conf_and_template_stay_in_sync():
    conf = (ROOT / "deploy/proxy.conf").read_text(encoding="utf-8")
    template = (ROOT / "deploy/proxy.conf.template").read_text(encoding="utf-8")
    assert template.replace("server_name ${WEB_DOMAIN};", "server_name _;") == conf


@pytest.mark.parametrize("config_path", PROXY_CONFIGS, ids=lambda p: p.name)
def test_proxy_https_hsts_and_html_headers_respect_nginx_inheritance(config_path: Path):
    text = config_path.read_text(encoding="utf-8")
    servers = _extract_blocks(text, "server")
    assert len(servers) == 1
    _, server_body = servers[0]
    assert "listen 8443 ssl;" in server_body

    server_headers = _parse_add_headers(server_body)
    assert server_headers.get("Strict-Transport-Security") == (EXPECTED_HSTS, True)
    # Server level must not inject CSP/XFO/XCTO into CORE /api/* locations.
    for core_owned in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert core_owned not in server_headers

    locations = _extract_blocks(server_body, "location")
    assert len(locations) == 3

    web_ui_locations = [(hdr, body) for hdr, body in locations if hdr == "/"]
    assert len(web_ui_locations) == 1
    _, web_ui_body = web_ui_locations[0]
    web_ui_headers = _parse_add_headers(web_ui_body)
    assert web_ui_headers["Strict-Transport-Security"] == (EXPECTED_HSTS, True)
    assert web_ui_headers["Content-Security-Policy"] == (EXPECTED_CSP, True)
    assert web_ui_headers["X-Frame-Options"] == ("SAMEORIGIN", True)
    assert web_ui_headers["X-Content-Type-Options"] == ("nosniff", True)
    assert web_ui_headers["Referrer-Policy"] == ("no-referrer", True)
    assert _parse_hide_headers(web_ui_body) >= {
        "Content-Security-Policy",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
    }

    # Every other location must either inherit HSTS (no add_header at all) or
    # explicitly declare HSTS with `always`, and CORE locations must not add
    # duplicate CSP/XFO/XCTO headers.
    for hdr, body in locations:
        if hdr == "/":
            continue
        loc_headers = _parse_add_headers(body)
        assert loc_headers == {}, f"CORE location {hdr!r} should inherit HSTS and keep CORE headers untouched"


def test_web_ui_nginx_conf_sets_security_headers_on_every_location():
    text = WEB_UI_CONFIG.read_text(encoding="utf-8")
    servers = _extract_blocks(text, "server")
    assert len(servers) == 1
    _, server_body = servers[0]

    # Plain HTTP container must not emit HSTS; TLS proxy owns HSTS.
    assert "Strict-Transport-Security" not in text

    required = {
        "Content-Security-Policy": (EXPECTED_CSP, True),
        "X-Frame-Options": ("SAMEORIGIN", True),
        "X-Content-Type-Options": ("nosniff", True),
        "Referrer-Policy": ("no-referrer", True),
    }

    server_headers = _parse_add_headers(server_body)
    for name, expected in required.items():
        assert server_headers.get(name) == expected

    locations = _extract_blocks(server_body, "location")
    by_header = dict(locations)
    assert "= /index.html" in by_header
    assert "= /ui-version.json" in by_header

    # Any location that declares add_header (such as Cache-Control) overrides
    # server-level add_header in nginx and must explicitly repeat security headers.
    for hdr, body in locations:
        loc_headers = _parse_add_headers(body)
        if not loc_headers:
            continue
        for name, expected in required.items():
            assert loc_headers.get(name) == expected, f"location {hdr!r} missing {name}"

    assert "no-cache, no-store, must-revalidate" in _parse_add_headers(by_header["= /index.html"])["Cache-Control"][0]
    assert "no-cache, no-store, must-revalidate" in _parse_add_headers(by_header["= /ui-version.json"])["Cache-Control"][0]


def test_csp_allows_angular_module_ui_and_lab_iframe_without_external_cdns():
    directives = _parse_csp(EXPECTED_CSP)

    assert directives["default-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert directives["script-src-attr"] == ["'unsafe-hashes'", f"'{ANGULAR_ONLOAD_HASH}'"]

    assert {"'self'", "'unsafe-inline'"} <= set(directives["style-src"])
    assert {"'self'", "data:", "blob:"} <= set(directives["img-src"])
    assert {"'self'", "blob:"} <= set(directives["media-src"])
    assert {"'self'", "ws:", "wss:"} <= set(directives["connect-src"])
    # Module UI runs from same-origin /api/modules/ui/*; LAB iframe runs over HTTPS.
    assert directives["frame-src"] == ["'self'", "https:"]
    assert directives["frame-ancestors"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]

    # No external CDN domains, plain http:, or wildcard sources.
    for name, sources in directives.items():
        for src in sources:
            assert src != "*", f"Wildcard source in {name}"
            assert not src.startswith("http://"), f"Plain HTTP source in {name}: {src}"
            assert not src.startswith("https://"), f"External CDN origin in {name}: {src}"


# ── Live nginx:1.27-alpine syntax + HTTP/HTTPS header verification ──────────


def _docker_nginx_available() -> bool:
    if shutil.which("docker") is None or shutil.which("openssl") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", NGINX_IMAGE],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return probe.returncode == 0


def _run(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check:
        assert result.returncode == 0, f"{args}:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    return result


def _make_tls_and_crl(work: Path) -> tuple[Path, Path]:
    tls, revocation = work / "tls", work / "revocation"
    tls.mkdir()
    revocation.mkdir()
    _run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=node-ca",
        "-keyout", str(tls / "node-ca.key"), "-out", str(tls / "node-ca.crt"),
    )
    _run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=vertep.test",
        "-keyout", str(tls / "vertep.key"), "-out", str(tls / "vertep.crt"),
    )
    (work / "ca.cnf").write_text(
        "[ca]\ndefault_ca=ca\n[ca]\ndatabase=%s\ncrlnumber=%s\ndefault_md=sha256\ndefault_crl_days=1\n"
        % (work / "index.txt", work / "crlnumber"),
        encoding="utf-8",
    )
    (work / "index.txt").write_text("", encoding="utf-8")
    (work / "crlnumber").write_text("01\n", encoding="utf-8")
    _run(
        "openssl", "ca", "-config", str(work / "ca.cnf"), "-gencrl",
        "-keyfile", str(tls / "node-ca.key"), "-cert", str(tls / "node-ca.crt"),
        "-out", str(revocation / "node-ca.crl"),
    )
    for item in list(tls.iterdir()) + list(revocation.iterdir()):
        item.chmod(0o644)
    return tls, revocation


@pytest.mark.skipif(not _docker_nginx_available(), reason=f"needs docker ({NGINX_IMAGE}) and openssl")
def test_nginx_t_validates_all_shipped_configs(tmp_path: Path):
    tls, revocation = _make_tls_and_crl(tmp_path)
    rendered = tmp_path / "proxy-rendered.conf"
    rendered.write_text(
        (ROOT / "deploy/proxy.conf.template")
        .read_text(encoding="utf-8")
        .replace("${WEB_DOMAIN}", "vertep.test"),
        encoding="utf-8",
    )
    rendered.chmod(0o644)

    # 1. docker/web-ui/nginx.conf
    _run(
        "docker", "run", "--rm",
        "-v", f"{WEB_UI_CONFIG}:/etc/nginx/conf.d/default.conf:ro",
        NGINX_IMAGE, "nginx", "-t",
    )

    # 2. deploy/proxy.conf and rendered deploy/proxy.conf.template
    # Add dummy `core` host so nginx -t resolves `proxy_pass http://core:8080;`.
    for cfg in (ROOT / "deploy/proxy.conf", rendered):
        _run(
            "docker", "run", "--rm",
            "--add-host", "core:127.0.0.1",
            "-v", f"{cfg}:/etc/nginx/conf.d/default.conf:ro",
            "-v", f"{tls}:/etc/vertep/tls:ro",
            "-v", f"{revocation}:/etc/vertep/revocation:ro",
            NGINX_IMAGE, "nginx", "-t",
        )


@pytest.fixture(scope="module")
def live_nginx_stack(tmp_path_factory):
    if not _docker_nginx_available():
        pytest.skip(f"needs docker ({NGINX_IMAGE}) and openssl")

    work = tmp_path_factory.mktemp("ac8-nginx")
    tls, revocation = _make_tls_and_crl(work)

    # Minimal web-ui root mimicking built Angular panel assets.
    html = work / "html"
    html.mkdir()
    (html / "index.html").write_text("<!DOCTYPE html><html><body><app-root></app-root></body></html>\n", encoding="utf-8")
    (html / "ui-version.json").write_text('{"version":"test","commit_sha":"abc","api_major":1}\n', encoding="utf-8")
    (html / "main-12345678.js").write_text("console.log('ok');\n", encoding="utf-8")
    for f in html.iterdir():
        f.chmod(0o644)

    # Minimal nginx stub for CORE emitting the exact security headers of core/api/auth.py
    # and core/module/gateway.py (module UI sandbox CSP).
    core_csp = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:"
    module_ui_csp = (
        "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
    )
    core_conf = work / "core.conf"
    core_conf.write_text(
        "server {\n"
        "  listen 8080;\n"
        "  location = /api/health {\n"
        "    add_header X-Content-Type-Options nosniff always;\n"
        "    add_header X-Frame-Options DENY always;\n"
        "    add_header Referrer-Policy no-referrer always;\n"
        f'    add_header Content-Security-Policy "{core_csp}" always;\n'
        "    default_type application/json;\n"
        '    return 200 \'{"status":"HEALTHY"}\';\n'
        "  }\n"
        "  location = /api/session {\n"
        "    add_header X-Content-Type-Options nosniff always;\n"
        "    add_header X-Frame-Options DENY always;\n"
        "    add_header Referrer-Policy no-referrer always;\n"
        f'    add_header Content-Security-Policy "{core_csp}" always;\n'
        '    return 401 "Authentication required";\n'
        "  }\n"
        "  location ^~ /api/modules/ui/ {\n"
        "    add_header X-Content-Type-Options nosniff always;\n"
        "    add_header X-Frame-Options SAMEORIGIN always;\n"
        "    add_header Referrer-Policy no-referrer always;\n"
        f'    add_header Content-Security-Policy "{module_ui_csp}" always;\n'
        "    default_type text/html;\n"
        '    return 200 "<!DOCTYPE html><html><body>module-ui</body></html>";\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    core_conf.chmod(0o644)

    rendered_proxy = work / "proxy.conf"
    rendered_proxy.write_text(
        (ROOT / "deploy/proxy.conf.template")
        .read_text(encoding="utf-8")
        .replace("${WEB_DOMAIN}", "vertep.test"),
        encoding="utf-8",
    )
    rendered_proxy.chmod(0o644)

    suffix = uuid.uuid4().hex[:8]
    network = f"vertep-ac8-{suffix}"
    names = {
        "core": f"ac8-core-{suffix}",
        "web-ui": f"ac8-webui-{suffix}",
        "proxy": f"ac8-proxy-{suffix}",
    }
    _run("docker", "network", "create", network)
    try:
        _run(
            "docker", "run", "-d", "--name", names["core"],
            "--network", network, "--network-alias", "core",
            "-v", f"{core_conf}:/etc/nginx/conf.d/default.conf:ro",
            NGINX_IMAGE,
        )
        _run(
            "docker", "run", "-d", "--name", names["web-ui"],
            "--network", network, "--network-alias", "web-ui",
            "-p", "127.0.0.1::8080",
            "-v", f"{WEB_UI_CONFIG}:/etc/nginx/conf.d/default.conf:ro",
            "-v", f"{html}:/usr/share/nginx/html:ro",
            NGINX_IMAGE,
        )
        _run(
            "docker", "run", "-d", "--name", names["proxy"],
            "--network", network,
            "-p", "127.0.0.1::8443",
            "-v", f"{rendered_proxy}:/etc/nginx/conf.d/default.conf:ro",
            "-v", f"{tls}:/etc/vertep/tls:ro",
            "-v", f"{revocation}:/etc/vertep/revocation:ro",
            NGINX_IMAGE,
        )
        proxy_port = _run("docker", "port", names["proxy"], "8443").stdout.split(":")[-1].strip()
        web_ui_port = _run("docker", "port", names["web-ui"], "8080").stdout.split(":")[-1].strip()
        proxy_url = f"https://127.0.0.1:{proxy_port}"
        web_ui_url = f"http://127.0.0.1:{web_ui_port}"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{proxy_url}/api/health", verify=False, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            logs = _run("docker", "logs", names["proxy"], check=False)
            pytest.fail(f"proxy did not become ready:\n{logs.stdout}\n{logs.stderr}")

        yield {
            "proxy": proxy_url,
            "web_ui": web_ui_url,
            "core_csp": core_csp,
            "module_ui_csp": module_ui_csp,
        }
    finally:
        for name in names.values():
            _run("docker", "rm", "-f", name, check=False)
        _run("docker", "network", "rm", network, check=False)


def _assert_single_header(response: httpx.Response, name: str, expected: str) -> None:
    values = response.headers.get_list(name)
    assert values == [expected], f"{response.request.url} header {name}: got {values!r}, expected {[expected]!r}"


@pytest.mark.parametrize("path", ["/", "/jobs/2026-000123", "/lab", "/modules/brands"])
def test_live_https_html_and_spa_deep_links_have_security_headers_and_hsts(live_nginx_stack, path: str):
    resp = httpx.get(live_nginx_stack["proxy"] + path, verify=False, timeout=5)
    assert resp.status_code == 200
    assert "<app-root>" in resp.text
    _assert_single_header(resp, "strict-transport-security", EXPECTED_HSTS)
    _assert_single_header(resp, "content-security-policy", EXPECTED_CSP)
    _assert_single_header(resp, "x-frame-options", "SAMEORIGIN")
    _assert_single_header(resp, "x-content-type-options", "nosniff")
    _assert_single_header(resp, "referrer-policy", "no-referrer")
    assert "no-cache" in resp.headers.get("cache-control", "")


def test_live_https_web_ui_assets_keep_cache_control_and_single_security_headers(live_nginx_stack):
    version = httpx.get(live_nginx_stack["proxy"] + "/ui-version.json", verify=False, timeout=5)
    assert version.status_code == 200
    assert "no-cache" in version.headers.get("cache-control", "")
    _assert_single_header(version, "strict-transport-security", EXPECTED_HSTS)
    _assert_single_header(version, "content-security-policy", EXPECTED_CSP)
    _assert_single_header(version, "x-content-type-options", "nosniff")

    bundle = httpx.get(live_nginx_stack["proxy"] + "/main-12345678.js", verify=False, timeout=5)
    assert bundle.status_code == 200
    assert "immutable" in bundle.headers.get("cache-control", "")
    _assert_single_header(bundle, "strict-transport-security", EXPECTED_HSTS)
    _assert_single_header(bundle, "content-security-policy", EXPECTED_CSP)
    _assert_single_header(bundle, "x-content-type-options", "nosniff")


def test_live_https_api_and_module_ui_have_hsts_without_header_duplication(live_nginx_stack):
    # Regular CORE 200 response
    health = httpx.get(live_nginx_stack["proxy"] + "/api/health", verify=False, timeout=5)
    assert health.status_code == 200
    _assert_single_header(health, "strict-transport-security", EXPECTED_HSTS)
    _assert_single_header(health, "content-security-policy", live_nginx_stack["core_csp"])
    _assert_single_header(health, "x-frame-options", "DENY")
    _assert_single_header(health, "x-content-type-options", "nosniff")

    # CORE 401 response still carries HSTS (`always`)
    session = httpx.get(live_nginx_stack["proxy"] + "/api/session", verify=False, timeout=5)
    assert session.status_code == 401
    _assert_single_header(session, "strict-transport-security", EXPECTED_HSTS)
    _assert_single_header(session, "x-content-type-options", "nosniff")

    # Module UI sandboxed iframe response preserves its sandbox CSP + SAMEORIGIN
    mod_ui = httpx.get(live_nginx_stack["proxy"] + "/api/modules/ui/demo/1.0/index.html", verify=False, timeout=5)
    assert mod_ui.status_code == 200
    _assert_single_header(mod_ui, "strict-transport-security", EXPECTED_HSTS)
    _assert_single_header(mod_ui, "content-security-policy", live_nginx_stack["module_ui_csp"])
    _assert_single_header(mod_ui, "x-frame-options", "SAMEORIGIN")
    _assert_single_header(mod_ui, "x-content-type-options", "nosniff")

    # Proxy-generated 401 on mTLS route (no client cert) also carries HSTS
    mtls_denied = httpx.post(live_nginx_stack["proxy"] + "/api/workers/heartbeat", verify=False, timeout=5)
    assert mtls_denied.status_code == 401
    _assert_single_header(mtls_denied, "strict-transport-security", EXPECTED_HSTS)


def test_live_direct_web_ui_http_has_security_headers_and_no_hsts(live_nginx_stack):
    for path in ("/", "/jobs/2026-000123", "/ui-version.json", "/main-12345678.js"):
        resp = httpx.get(live_nginx_stack["web_ui"] + path, timeout=5)
        assert resp.status_code == 200
        _assert_single_header(resp, "content-security-policy", EXPECTED_CSP)
        _assert_single_header(resp, "x-frame-options", "SAMEORIGIN")
        _assert_single_header(resp, "x-content-type-options", "nosniff")
        _assert_single_header(resp, "referrer-policy", "no-referrer")
        assert "strict-transport-security" not in resp.headers
