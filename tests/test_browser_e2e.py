#!/usr/bin/env python3
"""Browser E2E smoke tests for Vertep Web UI V2."""
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
            "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "scheduler": {"pending": 0, "next_run": None},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {
                "llm": {"backend": "ollama", "options": ["ollama", "openai"], "env": "VERTEP_LLM_PROVIDER", "configured": True},
                "tts": {"backend": "none", "options": ["none", "mock", "piper", "kokoro"], "env": "TTS_PROVIDER", "configured": True},
            },
            "update": {"current_version": "0.0.1.19", "available_version": None, "state": "IDLE", "update_available": None},
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


def test_job_detail_view_and_edit():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        job_id = "test-job-001"
        job = {
            "job_id": job_id, "topic": "Тестове завдання", "character_id": "did_samogon",
            "status": "READY", "priority": 5, "created_at": "2026-09-07T12:00:00Z",
            "updated_at": "2026-09-07T12:30:00Z", "events": ["CREATED", "READY"],
            "retries": 0, "source": "web", "approved": True, "approval_status": "approved",
            "published_to": ["youtube"], "task_type": "image", "min_vram_mb": 4096,
            "max_retries": 3, "brand_id": "brand01", "aspect_ratio": "16:9",
            "output_preset": "youtube", "version": 1, "stages": {}, "scenes": [],
            "artifacts": []
        }
        saved_patch = []

        def handle_job_detail(route):
            if route.request.method == "PATCH":
                saved_patch.append(route.request.post_data_json)
            route.fulfill(json=job)

        page.route(f"**/api/jobs/{job_id}", handle_job_detail)
        page.route("**/api/jobs", lambda route: route.fulfill(json=[job]))
        page.goto(f"{BASE_URL}/jobs/{job_id}")

        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("text=Тестове завдання")).to_be_visible()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("READY")
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("07.09.2026")

        page.locator("[data-testid='edit-job-button']").click()
        expect(page.locator("[data-testid='edit-topic-input']")).to_be_visible()

        page.get_by_role("button", name="Скасувати").click()
        expect(page.locator("[data-testid='edit-topic-input']")).not_to_be_visible()

        assert errors == []
        browser.close()


def test_jobs_delete_button_is_present():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        jobs = [{"job_id": "job-1", "topic": "Test", "status": "READY",
                 "created_at": "2026-09-07T12:00:00Z", "priority": 5, "character_id": "c1"}]

        page.route("**/api/jobs", lambda route: route.fulfill(json=jobs))
        page.goto(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='jobs-page']")).to_be_visible()
        expect(page.locator("button:has-text('Видалити')").first).to_be_visible()
        browser.close()


def test_dashboard_job_status_counts():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "scheduler": {"pending": 0, "next_run": None},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {},
            "update": {"current_version": "0.0.1.19", "state": "IDLE"},
        }))
        page.route("**/api/workers", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs", lambda route: route.fulfill(json=[
            {"job_id": "1", "topic": "Job 1", "status": "READY", "created_at": "2026-09-07T10:00:00Z", "priority": 5, "character_id": "c1"},
            {"job_id": "2", "topic": "Job 2", "status": "FAILED", "created_at": "2026-09-07T10:00:00Z", "priority": 5, "character_id": "c1"},
            {"job_id": "3", "topic": "Job 3", "status": "RUNNING", "created_at": "2026-09-07T10:00:00Z", "priority": 5, "character_id": "c1"},
            {"job_id": "4", "topic": "Job 4", "status": "PAUSED", "created_at": "2026-09-07T10:00:00Z", "priority": 5, "character_id": "c1"},
            {"job_id": "5", "topic": "Job 5", "status": "CANCELLED", "created_at": "2026-09-07T10:00:00Z", "priority": 5, "character_id": "c1"},
        ]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='dashboard']")).to_be_visible()
        expect(page.locator("[data-testid='job-statuses']")).to_contain_text("В процесі")
        expect(page.locator("[data-testid='job-statuses']")).to_contain_text("Завершено")
        expect(page.locator("[data-testid='job-statuses']")).to_contain_text("Помилки")
        expect(page.locator("[data-testid='dashboard']")).to_contain_text("Призупинено")
        expect(page.locator("[data-testid='dashboard']")).to_contain_text("Скасовано")
        browser.close()


def test_settings_shows_user_friendly_system_info():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 5, "inflight": 1, "dead_letter": 0},
            "scheduler": {"pending": 0, "next_run": None},
            "orchestration": {"active_jobs": 3, "active_scenes": 1},
            "providers": {
                "llm": {"backend": "ollama", "options": ["ollama", "openai"], "env": "VERTEP_LLM_PROVIDER", "configured": True},
            },
            "update": {"current_version": "0.0.1.19", "available_version": "0.0.1.20", "state": "IDLE", "update_available": True},
        }))
        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        expect(page.locator("[data-testid='system-info']")).to_be_visible()
        expect(page.locator("[data-testid='system-info']")).to_contain_text("Нормальний")
        expect(page.locator("[data-testid='system-info']")).to_contain_text("0.0.1.19")
        expect(page.locator("[data-testid='system-info']")).to_contain_text("Ядро")
        expect(page.locator("[data-testid='system-info']")).to_contain_text("База даних")
        expect(page.locator("[data-testid='backends-table']")).to_be_visible()
        assert errors == []
        browser.close()


def test_settings_shows_resources_or_unavailable():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "resources": {"cpu": 45, "ram": 62, "disk": 30},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {},
            "update": {"current_version": "0.0.1.19", "state": "IDLE"},
        }))
        page.route("**/api/workers", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs", lambda route: route.fulfill(json=[]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='resources']")).to_be_visible()
        browser.close()


def test_settings_update_shows_correct_version():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "providers": {},
            "update": {"current_version": "0.0.1.19", "available_version": "0.0.1.20", "state": "IDLE", "update_available": True},
        }))
        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='status-update-current-version']")).to_have_text("0.0.1.19")
        expect(page.locator("[data-testid='status-update-available-version']")).to_have_text("0.0.1.20")
        browser.close()


def test_dashboard_architecture_shows_core_and_workers():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "providers": {
                "llm": {"configured": True},
                "tts": {"configured": False},
            },
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "update": {"current_version": "0.0.1.19", "state": "IDLE"},
        }))
        page.route("**/api/workers", lambda route: route.fulfill(json=[
            {"node_id": "w1", "node_name": "GPU-Node-1", "role": "gpu", "status": "ONLINE",
             "capabilities": ["image_generation", "video_generation"]},
            {"node_id": "w2", "node_name": "Text-Node-1", "role": "text", "status": "ONLINE",
             "capabilities": ["llm"]},
        ]))
        page.route("**/api/jobs", lambda route: route.fulfill(json=[]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='architecture']")).to_be_visible()
        expect(page.locator("[data-testid='architecture']")).to_contain_text("CORE")
        expect(page.locator("[data-testid='architecture']")).to_contain_text("GPU")
        expect(page.locator("[data-testid='architecture']")).to_contain_text("Текст")
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
