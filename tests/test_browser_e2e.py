#!/usr/bin/env python3
"""Minimal browser E2E smoke tests for Vertep Web UI."""
import os
import sys

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    from unittest import SkipTest
    raise SkipTest("Playwright is not installed. Install with: pip install playwright")


BASE_URL = os.getenv("VERTEP_URL", "http://127.0.0.1:8080")


def test_setup_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE_URL}/setup?token=ci")
        assert "Vertep" in page.title()
        expect(page.locator("body")).to_contain_text("Перший запуск")
        browser.close()


def test_dashboard_loads_and_navigation_works_without_javascript_errors():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE_URL)
        expect(page.locator("#health")).to_contain_text("Ядро працює")
        page.locator('#nav button[data-panel="jobs"]').click()
        expect(page.locator("#jobs")).to_be_visible()
        assert errors == []
        browser.close()


def test_character_create_and_edit_use_localized_form():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        saved = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        character = {
            "id": "did_samogon", "name": "Дід Самогонщик", "language": "uk",
            "enabled": True, "system_prompt": "Говорить українською.",
            "voice": {"provider": "none", "language": "uk", "voice": None},
            "visual": {"style": "тепла ілюстрація", "aspect_ratio": "16:9"},
            "generation": {"workflow": "workflows/image/demo.json", "min_vram_mb": 4096,
                           "max_retries": 3},
            "publishing": {"enabled": False, "channels": []},
        }
        def handle_character(route):
            if route.request.method == "PUT":
                saved.append(route.request.post_data_json)
            route.fulfill(json=character)
        page.route("**/api/characters/did_samogon", handle_character)
        page.goto(BASE_URL)
        page.locator('#nav button[data-panel="characters"]').click()
        expect(page.locator("#characters")).to_be_visible()
        page.get_by_role("button", name="Новий персонаж").click()
        expect(page.locator("#chardialog")).to_be_visible()
        expect(page.locator("#characterform")).to_be_visible()
        expect(page.locator("#charjson")).to_be_hidden()
        expect(page.get_by_label("Ім’я персонажа")).to_have_value("Новий персонаж")
        page.get_by_role("button", name="Скасувати").click()
        page.evaluate("editCharacter('did_samogon')")
        expect(page.locator("#chardialog")).to_be_visible()
        expect(page.get_by_label("Ім’я персонажа")).to_have_value("Дід Самогонщик")
        expect(page.get_by_label("Системний ідентифікатор")).to_be_disabled()
        page.get_by_label("Ім’я персонажа").fill("Дід Самогонщик оновлений")
        page.get_by_role("button", name="Зберегти").click()
        expect(page.locator("#chardialog")).to_be_hidden()
        assert saved[0]["name"] == "Дід Самогонщик оновлений"
        assert errors == []
        browser.close()


def test_worker_wizard_role_labels_are_ukrainian():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL)
        page.locator('#nav button[data-panel="workers"]').click()
        page.get_by_role("button", name="Додати вузол").click()
        expect(page.locator("#addworkerdialog")).to_be_visible()
        expect(page.locator("#addworkerdialog h2")).to_have_text("Додати вузол")
        expect(page.locator("#workerrole option")).to_have_text([
            "GPU-вузол", "Текстовий вузол", "Голосовий вузол", "Вузол публікації",
            "Вузол резервного копіювання", "Вузол моніторингу",
        ])
        browser.close()


def test_friendly_queue_workflow_and_core_role_controls():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE_URL)

        page.locator('#nav button[data-panel="queue"]').click()
        expect(page.locator("#queuestatus")).to_be_hidden()
        expect(page.locator("#queue-friendly .friendly-card")).to_have_count(5)

        page.locator('#nav button[data-panel="workflows"]').click()
        page.locator("#workflows > button").click()
        expect(page.locator("#workflowdialog")).to_be_visible()
        expect(page.locator("#workflow-nodes .workflow-node")).to_have_count(1)
        expect(page.locator("#workflowjson")).to_have_count(0)
        page.locator("#workflow-close-friendly").click()

        page.locator('#nav button[data-panel="settings"]').click()
        expect(page.locator("#systemstatus")).to_be_hidden()
        expect(page.locator("#core-role-options input[type=checkbox]")).to_have_count(6)
        assert errors == []
        browser.close()


def test_health_endpoint_reports_core():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        response = page.request.get(f"{BASE_URL}/api/health")
        assert response.ok
        assert response.json()["service"] == "core"
        browser.close()


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[PASS] {name}")
            except Exception as exc:
                print(f"[FAIL] {name}: {exc}")
                failed += 1
    sys.exit(1 if failed else 0)
