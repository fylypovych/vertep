"""Monitoring module UI works inside the module iframe CSP without any CDN.

The module UI is served with core.module.gateway.UI_CSP (sandbox, default-src 'none', only inline
scripts/styles). Tailwind and Chart.js used to come from CDNs and were blocked; they are now built
into ui/index.html by scripts/build-monitoring-ui.mjs from ui-src/.
"""
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from core.module.gateway import UI_CSP

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules" / "monitoring"
PAGE = MODULE / "ui" / "index.html"


def test_the_page_loads_nothing_from_outside_and_matches_the_signed_digest():
    html = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"<(script|link|img|iframe)\b[^>]*\b(src|href)\s*=\s*[\"']?(https?:)?//", html, re.I)
    manifest = yaml.safe_load((MODULE / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["ui"]["files"]["index.html"] == hashlib.sha256(PAGE.read_bytes()).hexdigest()
    assert "Chart.js v4.5.1" in html and (MODULE / "ui-src" / "vendor" / "CHART_JS_LICENSE.md").is_file()


def test_the_built_page_is_reproducible_from_ui_src():
    node = shutil.which("node")
    if not node or not (ROOT / "web-v2" / "node_modules" / "@tailwindcss" / "node").is_dir():
        pytest.skip("needs node and web-v2/node_modules")
    result = subprocess.run([node, "scripts/build-monitoring-ui.mjs", "--check"], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_styles_and_charts_work_under_the_module_iframe_csp():
    playwright = pytest.importorskip("playwright.sync_api")
    url = "http://module.test/api/modules/ui/monitoring/0.0.4.16/index.html"
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            violations: list[str] = []
            page.on("console", lambda message: violations.append(message.text)
                    if "Content Security Policy" in message.text else None)
            # Only the page itself exists: any other request (a CDN) would fail.
            page.route("**/*", lambda route: route.fulfill(
                status=200, body=PAGE.read_bytes(), content_type="text/html; charset=utf-8",
                headers={"Content-Security-Policy": UI_CSP}) if route.request.url == url else route.abort())
            page.goto(url)
            assert page.evaluate("typeof Chart") == "function"
            # Tailwind utilities are present: the stats grid is a grid, hidden elements are hidden.
            assert page.evaluate("getComputedStyle(document.getElementById('statsCards')).display") == "grid"
            assert page.evaluate("""() => {
                const canvas = document.createElement('canvas');
                document.body.appendChild(canvas);
                const chart = new Chart(canvas, {type: 'line', data: {labels: ['a', 'b'],
                    datasets: [{label: 'x', data: [1, 2]}]}, options: {animation: false}});
                const drawn = chart.data.datasets[0].data.length === 2 && canvas.width > 0;
                chart.destroy();
                return drawn;
            }""")
            assert not [text for text in violations if "script-src" in text or "style-src" in text], violations
        finally:
            browser.close()
