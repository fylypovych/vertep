#!/usr/bin/env python3
"""Minimal browser E2E smoke tests for Vertep Web UI V2."""
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
        page.route("**/api/setup", lambda route: route.fulfill(json={
            "configured": False, "selected_role": None, "hardware": {},
            "roles": {"core": {"label": "Основний сервер", "modules": [], "capabilities": []}},
        }))
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
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.17",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "scheduler": {"pending": 0, "next_run": None},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {
                "llm": {"backend": "ollama", "options": ["ollama", "openai"], "env": "VERTEP_LLM_PROVIDER", "configured": True},
                "tts": {"backend": "none", "options": ["none", "mock", "piper", "kokoro"], "env": "TTS_PROVIDER", "configured": True},
            },
            "update": {"current_version": "0.0.1.17", "available_version": None, "state": "IDLE", "update_available": None},
        }))
        page.route("**/api/workers", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs", lambda route: route.fulfill(json=[]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='dashboard']")).to_be_visible()
        expect(page.locator("[data-testid='stat-workers']")).to_contain_text("Воркери")
        expect(page.locator("[data-testid='stat-system-state']")).to_contain_text("Нормальний")
        expect(page.get_by_role("link", name="Завдання")).to_be_visible()

        page.get_by_role("link", name="Завдання").click()
        expect(page).to_have_url(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='jobs-page']")).to_be_visible()
        expect(page.locator("[data-testid='create-job-button']")).to_contain_text("Нове завдання")

        page.get_by_role("link", name="Воркери").click()
        expect(page).to_have_url(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()
        expect(page.locator("[data-testid='create-worker-button']")).to_contain_text("Додати вузол")

        page.get_by_role("link", name="Персонажі").click()
        expect(page).to_have_url(f"{BASE_URL}/characters")
        expect(page.locator("[data-testid='characters-page']")).to_be_visible()
        expect(page.locator("[data-testid='create-character-button']")).to_contain_text("Новий персонаж")

        page.get_by_role("link", name="Налаштування").click()
        expect(page).to_have_url(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        expect(page.locator("[data-testid='backends-table']")).to_be_visible()

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
        page.route("**/api/characters", lambda route: route.fulfill(json=[character]))
        page.goto(f"{BASE_URL}/characters")
        expect(page.locator("[data-testid='characters-page']")).to_be_visible()

        page.locator("[data-testid='create-character-button']").click()
        expect(page.locator("[data-testid='character-modal']")).to_be_visible()
        expect(page.locator("[data-testid='character-name-input']")).to_have_value("Новий персонаж")
        expect(page.locator("[data-testid='character-id-input']")).to_be_disabled()

        page.get_by_role("button", name="Скасувати").click()
        expect(page.locator("[data-testid='character-modal']")).not_to_be_visible()

        page.locator("[data-testid='characters-page']").get_by_role("button", name="Редагувати").first.click()
        expect(page.locator("[data-testid='character-modal']")).to_be_visible()
        expect(page.locator("[data-testid='character-name-input']")).to_have_value("Дід Самогонщик")

        page.locator("[data-testid='character-name-input']").fill("Дід Самогонщик оновлений")
        page.get_by_role("button", name="Зберегти").click()
        expect(page.locator("[data-testid='character-modal']")).not_to_be_visible()
        assert saved[0]["name"] == "Дід Самогонщик оновлений"
        assert errors == []
        browser.close()


def test_worker_wizard_role_labels_are_ukrainian():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()

        page.locator("[data-testid='create-worker-button']").click()
        expect(page.locator("[data-testid='worker-wizard-modal']")).to_be_visible()
        expect(page.locator("[data-testid='worker-wizard-modal'] h3")).to_have_text("Додати вузол")
        expect(page.locator("[data-testid='worker-role-select']")).to_have_value("gpu")
        expect(page.locator("[data-testid='worker-role-select'] option")).to_have_count(6)
        browser.close()


def test_jobs_list_shows_empty_state_and_create_form():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/api/jobs", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='jobs-page']")).to_be_visible()
        expect(page.locator("[data-testid='jobs-empty']")).to_contain_text("Завдань не знайдено")
        expect(page.locator("[data-testid='create-job-button']")).to_be_visible()

        page.locator("[data-testid='create-job-button']").click()
        expect(page.locator("[data-testid='create-job-modal']")).to_be_visible()
        expect(page.locator("[data-testid='job-topic-input']")).to_be_visible()
        browser.close()


def test_settings_shows_system_status_backends_and_update():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.17",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "scheduler": {"pending": 0, "next_run": None},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {
                "llm": {"backend": "ollama", "options": ["ollama", "openai"], "env": "VERTEP_LLM_PROVIDER", "configured": True},
            },
            "update": {"current_version": "0.0.1.17", "available_version": None, "state": "IDLE", "update_available": None},
        }))
        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        expect(page.locator("[data-testid='backends-table']")).to_be_visible()
        expect(page.locator("[data-testid='backends-table']")).to_contain_text("ollama")
        expect(page.locator("[data-testid='update-unavailable']")).not_to_be_visible()
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
