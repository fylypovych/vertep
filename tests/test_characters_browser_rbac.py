"""#499 AC7: owner / non-owner / administrator for the installed Characters module,
in the Browser, through the real CORE process and its module Gateway.

A real CORE (uvicorn, disposable PostgreSQL) installs and activates the bundled
Characters package; the module's own HTTP service gets its token from CORE like
a Worker-started container and its database credentials from CORE; signed-in
administrator and brand moderators work through ``/api/modules/characters/v1``
and open the module page in the panel.
"""

import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import httpx
import pytest

from tests.test_install_join_modules_e2e import SETUP, module_runtime_env  # noqa: F401  (fixture)
from tests.test_node_process_acceptance import (  # noqa: F401  (fixtures)
    config_contour, module_postgres, node_dependencies, redis_url, requires_openssl)

PROFILE = {"name": "Герой", "language": "uk", "enabled": True, "system_prompt": "Повний профіль",
           "voice": {"provider": "none"}, "visual": {"style": "doc"}, "generation": {"steps": 20},
           "publishing": {"enabled": False}}


@requires_openssl
@pytest.mark.parametrize("config_contour", ["fresh"], indirect=True)
def test_owner_non_owner_and_admin_through_the_gateway_in_the_browser(module_runtime_env, config_contour,
                                                                      tmp_path, monkeypatch):
    from tests.test_media_browser_e2e import sync_playwright, expect
    from tests.test_provider_process_acceptance import PASSWORD
    c = config_contour
    url, api = c["url"], c["api"]
    done = httpx.post(url + "/api/setup/complete", headers=SETUP, timeout=60, json={
        "node_role": "core", "installation_name": "CORE", "username": "admin", "password": PASSWORD,
        "password_confirmation": PASSWORD, "ai_backend": "skip"})
    assert done.status_code == 200, done.text
    for brand in ("brand-a", "brand-b"):
        assert api.post("/api/brands", json={"id": brand, "name": brand}).status_code == 200
    for user, brand in (("mod-a", "brand-a"), ("mod-b", "brand-b")):
        created = api.post("/api/settings/users", json={"user": user, "role": "moderator", "brands": [brand],
                                                        "password": f"{user}-password-123"})
        assert created.status_code == 200, created.text

    # -- the node that runs the module container: enrollment and its own service ----
    from core.node_registry import create_node_csr
    token = api.post("/api/nodes/registration-tokens", json={"role": "text"}).json()["token"]
    enrolled = httpx.post(url + "/api/nodes/register", timeout=30, json={
        "registration_token": token, "node_id": "cpu-node", "csr": create_node_csr("cpu-node", tmp_path / "pki"),
        "capabilities": ["text_generation"], "version": "test", "enrollment_id": "cpu-node-characters",
        "hardware": {"gpu_available": False, "vram_mb": 0, "cpu_cores": 4, "ram_mb": 8192}})
    assert enrolled.status_code == 200, enrolled.text
    node_auth = {"X-Vertep-Token": enrolled.json()["jwt"]}
    monkeypatch.setenv("VERTEP_CORE_URL", url)
    monkeypatch.setenv("VERTEP_MODULE_TOKEN", "pending")
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "module-data" / "characters"))
    from modules.characters import characters_logic, characters_module
    from core.module.lifecycle import bundled_manifest
    for target in (characters_logic, characters_module):
        monkeypatch.setattr(target, "CORE", url)
    server = ThreadingHTTPServer(("127.0.0.1", 0), characters_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    inventory = [{**bundled_manifest("characters"), "ready": True, "endpoint": f"http://127.0.0.1:{server.server_address[1]}"}]
    stop = threading.Event()

    def heartbeat():
        return httpx.post(url + "/api/workers/heartbeat", headers=node_auth, timeout=30, json={
            "node_name": "cpu-node", "role": "text", "status": "FREE", "runtime_instance_id": "cpu-node-1",
            "capabilities": ["text_generation"], "gpu_available": False, "vram_mb": 0, "cpu_cores": 4,
            "ram_mb": 8192, "module_inventory": inventory,
            "self_test": {"status": "PASSED", "role": "text", "checked_at": datetime.now(timezone.utc).isoformat()}})

    def keep_reporting():
        while not stop.wait(1):
            heartbeat()

    assert heartbeat().status_code == 200
    reporter = threading.Thread(target=keep_reporting, daemon=True)
    reporter.start()
    try:
        # -- the administrator installs, places, approves and activates the module ---
        assert api.post("/api/nodes/module-registry/lifecycle/install", json={"bundled": "characters"}).status_code == 200
        assert api.put("/api/nodes/module-registry/placement/characters", json={"node_ids": ["cpu-node"]}).status_code == 200
        approval = api.put("/api/nodes/module-registry/approval/characters",
                           json={"permissions": ["characters.read", "characters.write", "characters.collaborate"]})
        assert approval.status_code == 200, approval.text
        activated = api.post("/api/nodes/module-registry/lifecycle/activate", json={"module_id": "characters"})
        assert activated.status_code == 200 and activated.json()["state"] == "ACTIVE", activated.text
        # The container's token and database credentials come from CORE.
        issued = httpx.post(url + "/api/modules/token", headers=node_auth, timeout=30,
                            json={"node_name": "cpu-node", "module_id": "characters"})
        assert issued.status_code == 200, issued.text
        for target in (characters_logic, characters_module):
            monkeypatch.setattr(target, "TOKEN", issued.json()["token"])
        monkeypatch.setitem(characters_logic._DB, "connection", None)
        characters_logic.CharactersModule()  # provisions its tables in its own schema via CORE credentials

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            def session(user, password):
                context = browser.new_context()
                auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
                assert context.request.post(url + "/api/session", headers={"Authorization": auth}).status == 200
                csrf = next(cookie["value"] for cookie in context.cookies() if cookie["name"] == "vertep_csrf")
                return context, {"x-csrf-token": csrf}

            def module_api(context, headers, method, path, body=None):
                response = context.request.fetch(url + "/api/modules/characters/v1" + path, method=method,
                                                 headers=headers, data=json.dumps(body) if body is not None else None)
                return response.status

            admin, admin_h = session("admin", PASSWORD)
            mod_a, mod_a_h = session("mod-a", "mod-a-password-123")
            mod_b, mod_b_h = session("mod-b", "mod-b-password-123")
            try:
                headers_b = {**mod_b_h, "content-type": "application/json"}
                headers_a = {**mod_a_h, "content-type": "application/json"}
                headers_admin = {**admin_h, "content-type": "application/json"}
                assert module_api(mod_b, headers_b, "POST", "/characters?brand_id=brand-b", {"id": "hero", **PROFILE}) == 201
                assert module_api(mod_a, headers_a, "POST", "/characters?brand_id=brand-b", {"id": "spy", **PROFILE}) == 403
                assert module_api(mod_a, headers_a, "PUT", "/characters/hero?brand_id=brand-a", {"name": "Чужий"}) == 409
                assert module_api(mod_a, headers_a, "POST", "/characters/hero/activate?brand_id=brand-b") == 403
                assert module_api(mod_b, headers_b, "POST", "/characters/hero/activate?brand_id=brand-b") == 200
                assert module_api(admin, headers_admin, "POST", "/characters?brand_id=brand-a", {"id": "admin-made", **PROFILE}) == 201
                assert module_api(admin, headers_admin, "PUT", "/characters/hero?brand_id=brand-b", {"name": "Герой 2"}) == 200
                listed = mod_a.request.get(url + "/api/modules/characters/v1/characters?brand_id=brand-a").json()
                assert {item["id"] for item in listed} == {"hero", "admin-made"}  # active foreign + own brand
                assert next(item for item in listed if item["id"] == "hero")["is_owner"] is False
                for context in (admin, mod_a, mod_b):
                    page = context.new_page()
                    page.goto(url + "/modules/characters")
                    expect(page.get_by_test_id("module-extension-frame")).to_be_visible(timeout=20000)
                    page.close()

                # The panel page "Персонажі" works only through the module (0.0.4.42): create with
                # the owner brand, reload, edit, and a duplicate ID keeps the editor open.
                page = admin.new_page()
                page.goto(url + "/characters")
                create = page.get_by_test_id("create-character-button")
                expect(create).to_be_enabled(timeout=20000)
                create.click()
                modal = page.get_by_test_id("character-modal")
                page.get_by_test_id("character-id-input").fill("panel-made")
                page.get_by_test_id("character-name-input").fill("З панелі")
                page.get_by_placeholder("vertep").fill("brand-a")
                page.get_by_role("button", name="Зберегти").click()
                expect(modal).to_be_hidden()
                made = admin.request.get(url + "/api/modules/characters/v1/characters/panel-made?brand_id=brand-a").json()
                assert made["name"] == "З панелі" and made["owner_brand_id"] == "brand-a"
                page.reload()
                page.get_by_test_id("characters-search").fill("panel-made")
                page.get_by_role("button", name="Редагувати").first.click()
                page.get_by_test_id("character-name-input").fill("З панелі 2")
                page.get_by_role("button", name="Зберегти").click()
                expect(modal).to_be_hidden()
                assert admin.request.get(url + "/api/modules/characters/v1/characters/panel-made?brand_id=brand-a").json()["name"] == "З панелі 2"
                page.get_by_test_id("create-character-button").click()
                page.get_by_test_id("character-id-input").fill("panel-made")
                page.get_by_test_id("character-name-input").fill("Дубль")
                page.get_by_role("button", name="Зберегти").click()
                expect(modal).to_be_visible()
                page.close()
            finally:
                browser.close()
    finally:
        stop.set()
        reporter.join(timeout=5)
        server.shutdown()
