"""i.0.0.4.93 (#493) AC4/AC7: signed module UI extension isolation in a real browser.

Real CORE on a socket, the production web-v2 bundle in Chromium and a real
HTTP module process. A working extension renders in a sandboxed iframe that
cannot reach the panel; a tampered extension and a deactivated one degrade to
a notice while the rest of the panel keeps working.
"""

import hashlib
import threading
from http.server import ThreadingHTTPServer

from tests.test_media_browser_e2e import browser, core, expect  # noqa: F401  (fixtures)
from tests.test_module_trust_isolation import UI_HTML, ModuleHandler, app_manifest

PROBE_HTML = (b"<!doctype html><html><body><h1 id='ext'>Extension OK</h1><p id='probe'></p><script>"
              b"var r=[];try{r.push(parent.document.title?'parent-dom':'none')}catch(e){r.push('parent-blocked')}"
              b"try{r.push(document.cookie?'cookie':'no-cookie')}catch(e){r.push('no-cookie')}"
              b"document.getElementById('probe').textContent=r.join(',')</script></body></html>")


def test_signed_ui_extension_is_isolated_and_failures_stay_local(core, browser, monkeypatch, tmp_path):  # noqa: F811
    from core.module.instances import ModuleInstanceRegistry, PlacementStore
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModuleHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ModuleHandler.ui, ModuleHandler.seen = PROBE_HTML, []
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.3.88"}, PlacementStore(tmp_path / "p.json"))

    class Runtime:
        modules = registry
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: Runtime)

    def extension(name, label, digest):
        ui = {"entry": "index.html", "navigation": [{"id": name, "label": label}], "files": {"index.html": digest}}
        return {**app_manifest(name, ui=ui), "ready": True, "endpoint": endpoint}
    registry.report_node("app-node", [extension("notes", "Нотатки", hashlib.sha256(PROBE_HTML).hexdigest()),
                                      extension("broken", "Зламане", hashlib.sha256(UI_HTML).hexdigest())])
    for name in ("notes", "broken"):
        registry.set_placement(name, ["app-node"])
    page = browser["page"]
    # The stand-in runtime above has no lifecycle; the menu's catalog read (Бекапи) gets an empty catalog.
    page.route("**/api/nodes/module-catalog", lambda route: route.fulfill(json={"modules": [], "nodes": []}))
    try:
        page.goto(f"{browser['url']}/")
        nav = page.get_by_test_id("sidebar-nav")
        expect(nav.get_by_role("link", name="Нотатки")).to_be_visible()
        nav.get_by_role("link", name="Нотатки").click()
        frame_element = page.get_by_test_id("module-extension-frame")
        expect(frame_element).to_be_visible()
        assert frame_element.get_attribute("sandbox") == "allow-scripts"
        frame = page.frame_locator("[data-testid=module-extension-frame]")
        expect(frame.locator("#ext")).to_have_text("Extension OK")
        expect(frame.locator("#probe")).to_have_text("parent-blocked,no-cookie")

        # Bytes no longer match the signed digest: only this view degrades.
        nav.get_by_role("link", name="Зламане").click()
        expect(page.get_by_test_id("module-extension-unavailable")).to_be_visible()
        expect(page.get_by_test_id("module-extension-frame")).to_have_count(0)
        nav.get_by_role("link", name="Вузли").click()
        expect(page).to_have_url(f"{browser['url']}/workers")

        # Deactivation on the node (instance stopped) removes the entry and access.
        registry.report_node("app-node", [extension("broken", "Зламане", hashlib.sha256(UI_HTML).hexdigest())])
        page.goto(f"{browser['url']}/modules/notes")
        expect(page.get_by_test_id("module-extension-unavailable")).to_be_visible()
        expect(page.get_by_test_id("sidebar-nav").get_by_role("link", name="Нотатки")).to_have_count(0)
        expect(page.get_by_test_id("sidebar-nav").get_by_role("link", name="Дашборд")).to_be_visible()
    finally:
        server.shutdown()
        # The digest-mismatch preflight is an expected 502 for the tampered extension only.
        browser["console_errors"][:] = [m for m in browser["console_errors"] if "502" not in m]
