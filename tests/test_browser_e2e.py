#!/usr/bin/env python3
"""Browser E2E smoke tests for Vertep Web UI V2."""
import os
import socket
import sys
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    from unittest import SkipTest
    raise SkipTest("Playwright is not installed. Install with: pip install playwright")


BASE_URL = os.getenv("VERTEP_URL", "http://127.0.0.1:8080")


def _has_live_server() -> bool:
    """Return True only when the app host/port is reachable."""
    import urllib.parse

    parsed = urllib.parse.urlparse(BASE_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _has_live_server(),
    reason=f"Browser E2E requires a running app at {BASE_URL}; skipping without an active server.",
)

# Standard /api/status mock used by sidebar and header on every page load.
DEFAULT_STATUS = {
    "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
    "version": "0.0.1.99",
    "system": {"state": "NORMAL"},
    "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
    "scheduler": {"pending": 0, "next_run": None},
    "orchestration": {"active_jobs": 0, "active_scenes": 0},
    "providers": {
        "llm": {"backend": "ollama", "options": [], "env": "", "configured": True},
        "tts": {"backend": "none", "options": [], "env": "", "configured": True},
    },
    "update": {"current_version": "0.0.1.99", "state": "IDLE"},
}


def _mock_status(page, overrides=None):
    """Register a /api/status route so sidebar/header never hit the real server."""
    data = {**DEFAULT_STATUS, **(overrides or {})}
    page.route("**/api/status", lambda route: route.fulfill(json=data))


def _mock_session(page, role="admin"):
    """Register /api/session so Auth/Admin guards pass deterministically in CI."""
    page.route("**/api/session", lambda route: route.fulfill(json={
        "authenticated": True, "user": "ci", "role": role,
    }))


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
        _mock_session(page)
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
        expect(page.get_by_role("link", name="Завдання", exact=True)).to_be_visible()

        page.get_by_role("link", name="Завдання", exact=True).click()
        expect(page).to_have_url(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='jobs-page']")).to_be_visible()
        expect(page.locator("[data-testid='create-job-button']")).to_contain_text("Нове завдання")

        page.locator("a[href='/workers']").click()
        expect(page).to_have_url(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()
        expect(page.locator("[data-testid='create-worker-button']")).to_contain_text("Додати вузол")

        page.get_by_role("link", name="Персонажі", exact=True).click()
        expect(page).to_have_url(f"{BASE_URL}/characters")
        expect(page.locator("[data-testid='characters-page']")).to_be_visible()
        expect(page.locator("[data-testid='create-character-button']")).to_contain_text("Новий персонаж")

        page.get_by_role("link", name="Налаштування", exact=True).click()
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
        _mock_status(page)
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
        _mock_status(page)
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
        _mock_session(page)
        _mock_status(page)
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
        _mock_status(page)

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
        _mock_status(page)

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
        _mock_session(page)
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
        _mock_session(page)
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


def test_logs_page_loads_empty():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='logs-page']")).to_be_visible()
        expect(page.locator("[data-testid='empty-state']")).to_contain_text("Логів не знайдено")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_logs_page_shows_entries():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(json=[
            {"level": "INFO", "message": "Core started", "timestamp": "2026-09-08T10:00:00Z", "node_name": "core"},
            {"level": "ERROR", "message": "Worker failed", "timestamp": "2026-09-08T10:01:00Z", "node_name": "gpu-1", "job_id": "j-001"},
        ]))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='logs-table']")).to_be_visible()
        expect(page.locator("[data-testid='logs-table']")).to_contain_text("Core started")
        expect(page.locator("[data-testid='logs-table']")).to_contain_text("Worker failed")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_logs_page_shows_api_error():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(status=500, json={"detail": "Internal error"}))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='error-state']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_logs_page_filter_level():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(json=[
            {"level": "ERROR", "message": "Only error", "timestamp": "2026-09-08T10:00:00Z"},
        ]))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='logs-table']")).to_be_visible()
        expect(page.locator("[data-testid='logs-table']")).to_contain_text("ERROR")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_health_endpoint_reports_core():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        response = page.request.get(f"{BASE_URL}/api/health")
        assert response.ok
        assert response.json()["service"] == "core"
        browser.close()

def test_job_detail_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/jobs/j-001", lambda route: route.fulfill(json={
            "job_id": "j-001", "topic": "Test Job", "status": "READY",
            "priority": 5, "created_at": "2026-09-08T10:00:00Z",
            "character_id": "char1", "stages": {}, "scenes": [],
            "artifacts": [], "events": [], "approved": False,
            "approval_status": "none", "published_to": [],
            "publication_results": {}, "version": 1,
            "active_task_ids": {}, "completed_task_ids": [],
            "source": "api", "retries": 0, "brand_id": "",
            "aspect_ratio": "9:16", "output_preset": "default",
            "task_type": "image", "min_vram_mb": 2048,
            "max_retries": 3,
        }))
        page.route("**/api/characters", lambda route: route.fulfill(json=[]))
        page.route("**/api/workflows", lambda route: route.fulfill(json=[]))
        page.route("**/api/channels/types", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs/j-001")
        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Test Job")
        expect(page.locator("[data-testid='delete-job-button']")).to_be_visible()
        expect(page.locator("[data-testid='edit-job-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_queue_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/tasks/queue**", lambda route: route.fulfill(json={
            "ready": [], "inflight": [],
        }))
        page.route("**/api/tasks/dead-letter**", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/queue")
        expect(page.locator("[data-testid='queue-page']")).to_be_visible()
        expect(page.locator("[data-testid='queue-page']")).to_contain_text("Виконання завдань")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_alerts_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/alerts**", lambda route: route.fulfill(json=[
            {"severity": "error", "type": "JOB_FAILED", "message": "Test failure", "job_id": "j-001"},
        ]))
        page.goto(f"{BASE_URL}/alerts")
        expect(page.locator("[data-testid='alerts-page']")).to_be_visible()
        expect(page.locator("[data-testid='alerts-page']")).to_contain_text("JOB_FAILED")
        expect(page.locator("[data-testid='alerts-page']")).to_contain_text("Test failure")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_health_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/health**", lambda route: route.fulfill(json={
            "status": "healthy", "service": "core", "jobs": 0,
            "checks": {"postgres": [True, "OK"]},
        }))
        page.route("**/api/metrics**", lambda route: route.fulfill(json={
            "jobs_total": 0, "queue_ready": 0, "queue_inflight": 0,
            "queue_dead_letter": 0, "jobs_scheduled": 0,
            "workers_online": 0, "jobs_by_status": {}, "scenes_by_status": {},
        }))
        page.route("**/api/health/history**", lambda route: route.fulfill(json={
            "history": [],
        }))
        page.goto(f"{BASE_URL}/health")
        expect(page.locator("[data-testid='health-page']")).to_be_visible()
        expect(page.locator("[data-testid='health-page']")).to_contain_text("Стан системи")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_published_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/jobs**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/published")
        expect(page.locator("[data-testid='published-page']")).to_be_visible()
        expect(page.locator("[data-testid='published-page']")).to_contain_text("Опубліковані матеріали")
        assert not errors, f"pageerror: {errors}"
        browser.close()





def test_workers_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/workers**", lambda route: route.fulfill(json=[
            {"node_id": "n1", "node_name": "GPU-Node-1", "role": "gpu", "status": "ONLINE",
             "capabilities": ["image_generation"], "vram_mb": 8192, "gpu_name": "RTX 4090"},
        ]))
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()
        expect(page.locator("[data-testid='workers-table']")).to_contain_text("GPU-Node-1")
        expect(page.locator("[data-testid='create-worker-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_worker_detail_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/nodes/n1**", lambda route: route.fulfill(json={
            "node_id": "n1", "node_name": "GPU-Node-1", "role": "gpu", "status": "ONLINE",
            "capabilities": ["image_generation"], "vram_mb": 8192, "gpu_name": "RTX 4090",
            "ram_mb": 16384, "disk_free_mb": 500000, "cpu_load": 15, "gpu_load": 45,
            "temperature": 65, "version": "0.0.1.22", "current_job": None, "current_task": None,
            "supported_tasks": ["image"], "supported_workflows": ["*"],
            "hardware": {"gpu_arch": "Ada Lovelace"}, "update_state": {},
        }))
        page.goto(f"{BASE_URL}/workers/n1")
        expect(page.locator("[data-testid='worker-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='worker-detail-page']")).to_contain_text("GPU-Node-1")
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_characters_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/characters**", lambda route: route.fulfill(json=[
            {"id": "char1", "name": "Дід Самогон", "language": "uk", "enabled": True,
             "system_prompt": "Test", "voice": {}, "visual": {}, "generation": {}, "publishing": {}},
        ]))
        page.goto(f"{BASE_URL}/characters")
        expect(page.locator("[data-testid='characters-page']")).to_be_visible()
        expect(page.locator("[data-testid='characters-page']")).to_contain_text("Дід Самогон")
        expect(page.locator("[data-testid='create-character-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_workflows_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/workflows**", lambda route: route.fulfill(json=[
            {"kind": "image", "name": "demo.json"},
        ]))
        page.route("**/api/characters**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/workflows")
        expect(page.locator("[data-testid='workflows-page']")).to_be_visible()
        expect(page.locator("[data-testid='workflows-table']")).to_contain_text("demo.json")
        expect(page.locator("[data-testid='create-workflow-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_brands_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/brands**", lambda route: route.fulfill(json=[
            {"id": "brand1", "name": "Test Brand", "enabled": True, "metadata": {}, "publishing": {}},
        ]))
        page.goto(f"{BASE_URL}/brands")
        expect(page.locator("[data-testid='brands-page']")).to_be_visible()
        expect(page.locator("[data-testid='brands-page']")).to_contain_text("Test Brand")
        expect(page.locator("[data-testid='create-brand-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_worker_wizard_opens_and_shows_token():
    """V2C-401: Worker onboarding wizard generates token with TTL."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/workers", lambda route: route.fulfill(json=[]))
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "postgres": "OK", "redis": "OK", "storage": "OK",
            "version": "0.0.1.99", "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "scheduler": {"pending": 0, "next_run": None},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {"llm": {"backend": "ollama", "options": [], "env": "", "configured": True},
                          "tts": {"backend": "none", "options": [], "env": "", "configured": True}},
            "update": {"current_version": "0.0.1.99", "state": "IDLE"},
        }))
        page.route("**/api/nodes/registration-tokens", lambda route: route.fulfill(json={
            "token": "VT-AAAA-BBBB-CCCC", "role": "gpu",
            "expires_at": "2026-09-08T12:00:00Z", "push_token": False,
        }))
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()
        page.locator("[data-testid='create-worker-button']").click()
        page.locator("[data-testid='generate-token-button']").click()
        expect(page.locator("text=VT-AAAA-BBBB-CCCC")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_worker_detail_shows_hardware_and_actions():
    """V2C-402/V2C-403: Worker detail shows typed hardware and action buttons."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)
        page.route("**/api/nodes/n1**", lambda route: route.fulfill(json={
            "node_id": "n1", "node_name": "GPU-Node-1", "role": "gpu", "status": "ONLINE",
            "capabilities": ["image_generation"], "vram_mb": 8192, "gpu_name": "RTX 4090",
            "ram_mb": 16384, "disk_free_mb": 500000, "cpu_load": 15, "gpu_load": 45,
            "temperature": 65, "version": "0.0.1.22", "current_job": None, "current_task": None,
            "supported_tasks": ["image"], "supported_workflows": ["*"],
            "hardware": {"gpu_arch": "Ada Lovelace", "cpu_count": 16},
            "update_state": {"desired_state": None, "update_target_version": None,
                             "rollback_target_version": None, "self_test_requested_at": None},
        }))
        page.goto(f"{BASE_URL}/workers/n1")
        expect(page.locator("[data-testid='worker-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='worker-detail-page']")).to_contain_text("GPU-Node-1")
        expect(page.locator("[data-testid='worker-detail-page']")).to_contain_text("RTX 4090")
        expect(page.locator("[data-testid='self-test-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()


def test_settings_roles_shows_deployment_status():
    """V2C-404: Settings roles section shows deployment state."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_session(page)
        _mock_status(page, {"update": {"current_version": "0.0.1.99", "state": "IDLE"}})
        page.route("**/api/system/roles", lambda route: route.fulfill(json={
            "node_role": "core", "active_roles": [],
            "available_roles": [
                {"id": "gpu", "label": "GPU Node", "services": ["comfyui"], "capabilities": ["image_generation"]},
                {"id": "text", "label": "Text Node", "services": ["ollama"], "capabilities": ["text_generation"]},
            ],
            "deployment": {"state": None, "error": None},
            "queued": False,
        }))
        # Catch-all for other settings API calls
        for pattern in ["**/api/secrets**", "**/api/models/**", "**/api/system/update/**",
                        "**/api/system/backups**", "**/api/settings/**", "**/api/security/**"]:
            page.route(pattern, lambda route: route.fulfill(json={}))
        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        page.locator("button", has_text="Ролі").first.click()
        expect(page.locator("[data-testid='roles-save-button']")).to_be_visible()
        assert not errors, f"pageerror: {errors}"
        browser.close()
def _script_job(job_id="job-script-01", status="SCRIPT_PENDING_APPROVAL"):
    """Minimal Job in script review state (i.0.0.0.4)."""
    return {
        "job_id": job_id, "topic": "Сценарій до затвердження", "character_id": "did_samogon",
        "priority": 5, "status": status, "created_at": "2026-09-08T10:00:00Z",
        "source": "web", "retries": 0, "approved": False, "approval_status": "pending",
        "published_to": [], "task_type": "image", "min_vram_mb": 4096, "max_retries": 3,
        "brand_id": "brand01", "aspect_ratio": "16:9", "output_preset": "youtube",
        "version": 1, "stages": {}, "scenes": [], "artifacts": [], "events": ["SCRIPT_PENDING_APPROVAL"],
        "script": {
            "title": "Новий ролик",
            "description": "Короткий опис ролика",
            "scenes": [
                {"prompt": "перша сцена", "voiceover": "озвучка 1", "duration": 3},
                {"prompt": "друга сцена", "voiceover": "озвучка 2", "duration": 4},
            ],
        },
    }


def test_script_approval_happy_path():
    """i.0.0.0.4: script approve posts to backend and removes the approve button."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_session(page)
        _mock_status(page)

        job = _script_job()
        approved_job = {**job, "status": "SCRIPT_APPROVED", "approved": True, "approval_status": "approved"}

        def handle_detail(route):
            if route.request.method == "POST":
                route.fulfill(json=approved_job)
            else:
                route.fulfill(json=job)

        page.route("**/api/jobs/job-script-01/script/approve", lambda route: route.fulfill(json=approved_job))
        page.route("**/api/jobs/job-script-01", handle_detail)
        page.goto(f"{BASE_URL}/jobs/job-script-01")

        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='job-script']")).to_be_visible()
        expect(page.locator("[data-testid='approve-script-button']")).to_be_visible()
        expect(page.locator("[data-testid='job-script']")).to_contain_text("Новий ролик")

        page.locator("[data-testid='approve-script-button']").click()
        expect(page.locator("[data-testid='approve-script-button']")).not_to_be_visible()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Сценарій затверджено")
        assert errors == [], f"pageerror: {errors}"
        browser.close()


def test_script_revision_happy_path():
    """i.0.0.0.4: script revision prompt posts revision to backend."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_session(page)
        _mock_status(page)

        revised = None
        job = _script_job()
        revised_job = {**job, "status": "SCRIPT_REVISION_REQUESTED"}

        def handle_revision(route):
            nonlocal revised
            revised = route.request.post_data_json
            route.fulfill(json=revised_job)

        page.on("dialog", lambda dialog: dialog.accept("зробити сцени коротшими"))
        page.route("**/api/jobs/job-script-01/script/revision", handle_revision)
        page.route("**/api/jobs/job-script-01", lambda route: route.fulfill(json=job))
        page.goto(f"{BASE_URL}/jobs/job-script-01")

        page.locator("[data-testid='revision-script-button']").click()
        assert revised is not None, "script/revision call was not made"
        assert revised.get("revision") == "зробити сцени коротшими", revised
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Запитані правки")
        assert errors == [], f"pageerror: {errors}"
        browser.close()
def test_script_backend_error_shows_error_and_no_crash():
    """i.0.0.0.4: backend error on approve surfaces an action error without crashing the page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_session(page)
        _mock_status(page)

        job = _script_job()

        def handle_approve(route):
            route.fulfill(status=500, content_type="application/json",
                          body='{"detail": "Сценарій недоступний для затвердження"}')

        page.route("**/api/jobs/job-script-01", lambda route: route.fulfill(json=job))
        page.route("**/api/jobs/job-script-01/script/approve", handle_approve)
        page.goto(f"{BASE_URL}/jobs/job-script-01")

        page.locator("[data-testid='approve-script-button']").click()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Сценарій недоступний для затвердження")
        assert errors == [], f"pageerror: {errors}"
        browser.close()


def _storyboard_job(job_id="job-sb-01"):
    """Minimal Job in storyboard review state with ready image previews (i.0.0.0.4)."""
    return {
        "job_id": job_id, "topic": "Розкадровка до затвердження", "character_id": "did_samogon",
        "priority": 5, "status": "STORYBOARD_PENDING_APPROVAL", "created_at": "2026-09-08T10:00:00Z",
        "source": "web", "retries": 0, "approved": False, "approval_status": "pending",
        "published_to": [], "task_type": "image", "min_vram_mb": 4096, "max_retries": 3,
        "brand_id": "brand01", "aspect_ratio": "16:9", "output_preset": "youtube",
        "version": 1, "stages": {}, "scenes": [], "artifacts": [], "events": ["STORYBOARD_PENDING_APPROVAL"],
        "active_storyboard_version": 1,
        "storyboards": [{
            "version": 1, "title": "Розкадровка v1", "description": "Опис",
            "hashtags": ["#test"], "status": "pending_approval", "created_at": "2026-09-08T10:00:00Z",
            "image_version": 1, "image_status": "ready",
            "scenes": [
                {"index": 1, "prompt": "кадр 1", "video_prompt": "рух 1", "voiceover": "голос 1",
                 "duration": 3, "scene_id": "s1", "image_artifact_id": "art-s1", "image_version": 1},
                {"index": 2, "prompt": "кадр 2", "video_prompt": "рух 2", "voiceover": "голос 2",
                 "duration": 4, "scene_id": "s2", "image_artifact_id": "art-s2", "image_version": 1},
            ],
        }],
    }


def test_storyboard_review_with_artifacts_and_approve():
    """i.0.0.0.4: storyboard renders scene previews from real artifacts and approves the storyboard."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)

        job = _storyboard_job()
        approved_job = {**job, "status": "STORYBOARD_APPROVED", "approved": True,
                        "approval_status": "approved"}

        page.route("**/api/jobs/job-sb-01", lambda route: route.fulfill(json=job))
        page.route("**/api/jobs/job-sb-01/artifacts/art-s1/download", lambda route: route.fulfill(body=b"x"))
        page.route("**/api/jobs/job-sb-01/artifacts/art-s2/download", lambda route: route.fulfill(body=b"x"))
        page.route("**/api/jobs/job-sb-01/storyboards/approve", lambda route: route.fulfill(json=approved_job))
        page.goto(f"{BASE_URL}/jobs/job-sb-01")

        expect(page.locator("[data-testid='job-storyboard']")).to_be_visible()
        expect(page.locator("[data-testid='job-storyboard']")).to_contain_text("Розкадровка v1")
        expect(page.locator("[data-testid='job-storyboard']")).to_contain_text("кадр 1")
        preview_link = page.locator("[data-testid='job-storyboard'] a[href*='/artifacts/art-s1/download']")
        expect(preview_link).to_be_visible()

        # Approve the storyboard via the general approve button (canReviewStoryboard).
        page.get_by_role("button", name="Схвалити", exact=True).click()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Розкадровка затверджена")
        assert errors == [], f"pageerror: {errors}"
        browser.close()


def test_storyboard_stale_version_conflict():
    """i.0.0.0.4: stale-version conflict (409) on image preview approval surfaces an error."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_status(page)

        job = _storyboard_job()

        def handle_images_approve(route):
            route.fulfill(status=409, content_type="application/json",
                          body='{"detail": "Storyboard version 1 is stale; active version is 2"}')

        page.route("**/api/jobs/job-sb-01", lambda route: route.fulfill(json=job))
        page.route("**/api/jobs/job-sb-01/storyboards/images/approve", handle_images_approve)
        page.goto(f"{BASE_URL}/jobs/job-sb-01")

        page.get_by_role("button", name="Затвердити превʼю розкадровки").click()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("stale")
        assert errors == [], f"pageerror: {errors}"
        browser.close()


# ── First Run Wizard Browser E2E ────────────────────────────────────

SETUP_ROLES = {
    "core": {"label": "Основний сервер", "modules": ["proxy", "core", "postgres"],
             "capabilities": ["scheduling", "api"]},
    "gpu": {"label": "GPU Worker", "modules": ["comfyui"],
            "capabilities": ["image_generation", "video_generation"]},
    "text": {"label": "Text Worker", "modules": ["ollama"],
             "capabilities": ["text_generation"]},
}


def _setup_api_response(configured=False, selected_role=None, hardware=None):
    return {
        "configured": configured, "selected_role": selected_role,
        "hardware": hardware or {"cpu": "x86_64", "ram_mb": 8192,
                                  "gpu": {"vendor": "none", "name": "N/A"},
                                  "docker_version": "24.0.7"},
        "roles": SETUP_ROLES,
    }


def _mock_setup(page, configured=False, selected_role=None, hardware=None):
    page.route("**/api/setup", lambda route: route.fulfill(
        json=_setup_api_response(configured, selected_role, hardware)))


def test_setup_wizard_navigates_all_steps():
    """Wizard step navigation: forward and backward through all sections."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_setup(page)
        page.goto(f"{BASE_URL}/setup?token=ci")
        expect(page.locator("body")).to_contain_text("Перший запуск")
        sections = page.locator("main section")
        count = sections.count()
        assert count >= 7, f"Expected at least 7 wizard sections, got {count}"
        back_btn = page.locator("#back")
        expect(back_btn).to_have_css("visibility", "hidden")
        for _ in range(count - 1):
            page.locator("#next").click()
            page.wait_for_timeout(100)
        expect(back_btn).to_have_css("visibility", "visible")
        page.locator("#back").click()
        page.wait_for_timeout(100)
        assert errors == [], f"pageerror: {errors}"
        browser.close()


def test_setup_wizard_core_hides_connection_fields():
    """Core role selection hides core connection form."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _mock_setup(page)
        page.goto(f"{BASE_URL}/setup?token=ci")
        core_conn = page.locator("#coreConnection")
        expect(core_conn).to_have_css("display", "none")
        page.locator("#nodeRole").select_option("gpu")
        page.wait_for_timeout(100)
        expect(core_conn).to_have_css("display", "block")
        expect(page.locator("#coreUrl")).to_be_visible()
        page.locator("#nodeRole").select_option("core")
        page.wait_for_timeout(100)
        expect(core_conn).to_have_css("display", "none")
        browser.close()


def test_setup_wizard_configured_redirects_home():
    """If already configured, setup page redirects to /."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _mock_setup(page, configured=True)
        page.goto(f"{BASE_URL}/setup?token=ci")
        page.wait_for_timeout(500)
        assert page.url.rstrip("/") == BASE_URL or page.url == f"{BASE_URL}/"
        browser.close()


def test_setup_wizard_shows_hardware():
    """Hardware section shows detected hardware info."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        hw = {"cpu": "AMD Ryzen 9", "ram_mb": 32768,
              "gpu": {"vendor": "nvidia", "name": "RTX 4090"},
              "docker_version": "24.0.7"}
        _mock_setup(page, hardware=hw)
        page.goto(f"{BASE_URL}/setup?token=ci")
        for _ in range(4):
            page.locator("#next").click()
            page.wait_for_timeout(100)
        expect(page.locator("#hardware")).to_contain_text("RTX 4090")
        browser.close()


def test_setup_wizard_health_check_displays_results():
    """Health check step fetches and renders check results."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _mock_session(page)
        _mock_setup(page)
        page.route("**/api/setup/health", lambda route: route.fulfill(json={
            "ready": True, "checks": {"core": "OK", "postgresql": "OK", "redis": "OK"}
        }))
        page.goto(f"{BASE_URL}/setup?token=ci")
        steps_count = page.locator("main section").count()
        for _ in range(steps_count - 2):
            page.locator("#next").click()
            page.wait_for_timeout(100)
        page.wait_for_timeout(500)
        expect(page.locator("#health")).to_contain_text("PostgreSQL")
        browser.close()


def test_setup_wizard_back_button_hidden_on_first_step():
    """Back button is invisible on step 0."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _mock_setup(page)
        page.goto(f"{BASE_URL}/setup?token=ci")
        expect(page.locator("#back")).to_have_css("visibility", "hidden")
        page.locator("#next").click()
        page.wait_for_timeout(100)
        expect(page.locator("#back")).to_have_css("visibility", "visible")
        browser.close()


def test_setup_wizard_password_minimum_length():
    """Password field has minlength=12 attribute."""
    html = Path("web/setup.html").read_text(encoding="utf-8")
    assert 'minlength="12"' in html


def _ready_job(job_id="job-ready-01", status="READY"):
    """Minimal Job in READY state (final/video approval + publication)."""
    return {
        "job_id": job_id, "topic": "Ролик готовий до затвердження", "character_id": "did_samogon",
        "priority": 5, "status": status, "created_at": "2026-09-08T10:00:00Z", "source": "web",
        "retries": 0, "approved": False, "approval_status": "pending", "approved_channels": [],
        "published_to": [], "task_type": "video", "min_vram_mb": 4096, "max_retries": 3,
        "brand_id": "brand01", "aspect_ratio": "16:9", "output_preset": "youtube",
        "version": 1, "stages": {}, "scenes": [], "artifacts": [], "events": [status],
        "script": {"title": "Ролик до затвердження", "scenes": [], "hashtags": [], "description": ""},
        "storyboards": [], "publication_results": {},
        "character": "did_samogon", "workflow": "img2vid", "channel_types": [],
    }


def test_final_video_approval_of_ready_job():
    """i.0.0.0.17: final (video) approval of a READY job posts /jobs/{id}/approve."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        console_errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.route("**/api/**", lambda route: route.fulfill(json={}))
        _mock_session(page)
        _mock_status(page)
        job = _ready_job("job-final-01")
        approved_job = {**job, "status": "PUBLISHING", "approved": True, "approval_status": "approved"}

        page.route("**/api/jobs/job-final-01/approve", lambda route: route.fulfill(json=approved_job))
        page.route("**/api/jobs/job-final-01", lambda route: route.fulfill(json=job))
        page.route("**/api/channels/types", lambda route: route.fulfill(json=["youtube", "tiktok"]))
        page.route("**/api/brands/**/channels", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs/job-final-01")

        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='approve-final-button']")).to_be_visible()
        page.locator("[data-testid='approve-final-button']").click()
        expect(page.locator("[data-testid='approve-final-button']")).not_to_be_visible()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Публікація")
        assert errors == [], f"pageerror: {errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        browser.close()
def test_publication_flow_launches_and_shows_result():
    """i.0.0.0.17: publication flow posts /jobs/{id}/publish and renders results."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        console_errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.route("**/api/**", lambda route: route.fulfill(json={}))
        _mock_session(page)
        _mock_status(page)
        job = _ready_job("job-ready-01")
        published_job = {
            **job, "status": "PUBLISHED", "approved": True,
            "published_to": ["youtube"],
            "publication_results": {
                "youtube": {"channel": "youtube", "status": "PUBLISHED", "url": "https://youtu.be/abc123", "error": None},
            },
        }

        page.route("**/api/jobs/job-ready-01/publish", lambda route: route.fulfill(json=published_job))
        page.route("**/api/jobs/job-ready-01", lambda route: route.fulfill(json=job))
        page.route("**/api/channels/types", lambda route: route.fulfill(json=["youtube", "tiktok"]))
        page.route("**/api/brands/**/channels", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs/job-ready-01")

        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='publish-confirm-button']")).to_be_visible()
        page.locator("[data-testid='publish-confirm-button']").click()
        expect(page.locator("[data-testid='publication-results']")).to_be_visible()
        expect(page.locator("[data-testid='publication-results']")).to_contain_text("YouTube")
        expect(page.locator("[data-testid='publication-results']")).to_contain_text("PUBLISHED")
        assert errors == [], f"pageerror: {errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        browser.close()


def test_fleet_enrollment_issues_token_and_exposes_register_endpoint():
    """i.0.0.0.17: fleet enrollment issues a registration token and shows the register URL."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        console_errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.route("**/api/**", lambda route: route.fulfill(json={}))
        _mock_session(page)
        _mock_status(page)
        requested_roles = []

        def handle_token(route):
            requested_roles.append(route.request.post_data_json)
            route.fulfill(json={
                "token": "VT-ENR-LL-ROLE01", "role": "gpu",
                "expires_at": "2026-09-08T12:00:00Z", "push_token": False,
            })

        page.route("**/api/nodes/registration-tokens", handle_token)
        page.route("**/api/workers", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()

        page.locator("[data-testid='create-worker-button']").click()
        page.locator("[data-testid='worker-role-select']").select_option("gpu")
        page.locator("[data-testid='generate-token-button']").click()
        expect(page.locator("[data-testid='token-display']")).to_contain_text("VT-ENR-LL-ROLE01")
        expect(page.locator("[data-testid='token-display']")).to_contain_text("/api/nodes/register")
        assert requested_roles, "registration-tokens POST was not made"
        assert errors == [], f"pageerror: {errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        browser.close()
def test_update_critical_mutations_install_and_canary():
    """i.0.0.0.17: update critical mutations (install / promote / rollback) hit the backend."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        console_errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        _mock_session(page)
        _mock_status(page)

        update_status = {
            "current_version": "0.0.1.53", "available_version": "0.0.2.0",
            "state": "IDLE", "update_available": True,
        }
        post_calls = []

        page.route("**/api/system/update", lambda route: route.fulfill(json=update_status))
        page.route("**/api/system/update/readiness", lambda route: route.fulfill(
            json={"ready": True, "inflight": 0, "busy_workers": []}))
        page.route("**/api/system/update/rolling", lambda route: route.fulfill(
            json={"state": "IDLE", "current_batch": 0, "total_batches": 0}))
        page.route("**/api/system/update/rolling/promote", lambda route: (post_calls.append("promote"), route.fulfill(json={"ok": True}))[1])
        page.route("**/api/system/update/rolling/rollback", lambda route: (post_calls.append("rollback"), route.fulfill(json={"ok": True}))[1])
        page.route("**/api/system/update/rolling/cancel", lambda route: (post_calls.append("cancel"), route.fulfill(json={"ok": True}))[1])
        page.route("**/api/system/update/run", lambda route: (post_calls.append("install"), route.fulfill(json=update_status))[1])
        page.route("**/api/system/recovery/normal", lambda route: (post_calls.append("recover"), route.fulfill(json={"state": "NORMAL"}))[1])

        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        page.locator("button", has_text="Оновлення").first.click()
        expect(page.locator("[data-testid='settings-update']")).to_be_visible()

        page.locator("[data-testid='update-install']").click()
        page.locator("[data-testid='update-promote-canary']").click()
        page.locator("[data-testid='update-rollback-canary']").click()
        page.locator("[data-testid='update-recover-normal']").click()

        assert "install" in post_calls, post_calls
        assert "promote" in post_calls, post_calls
        assert "rollback" in post_calls, post_calls
        assert "recover" in post_calls, post_calls
        assert errors == [], f"pageerror: {errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        browser.close()
def test_backup_critical_mutations_create_and_restore():
    """i.0.0.0.17: backup critical mutations (create / restore) hit the backend."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        console_errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        _mock_session(page)
        _mock_status(page)

        post_calls = []
        snapshots = [{"snapshot_id": "snap-1", "created_at": "2026-09-08T10:00:00Z", "state": "ok"}]

        def handle_backups(route):
            if route.request.method == "POST":
                post_calls.append("create")
                snapshots.append({"snapshot_id": "snap-2", "created_at": "2026-09-08T11:00:00Z", "state": "ok"})
            route.fulfill(json={"snapshots": snapshots})

        page.route("**/api/system/backups/*/restore/progress", lambda route: route.fulfill(json={"progress": 100, "message": "ok"}))
        page.route("**/api/system/backups", handle_backups)
        page.route("**/api/system/backups/*/restore", lambda route: (post_calls.append("restore"), route.fulfill(json={"ok": True}))[1])

        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        page.locator("button", has_text="Бекапи").first.click()
        expect(page.locator("[data-testid='settings-backup']")).to_be_visible()

        page.locator("[data-testid='backup-create-button']").click()
        expect(page.locator("[data-testid='backup-restore-button']").first).to_be_visible()
        page.locator("[data-testid='backup-restore-button']").first.click()
        page.get_by_role("button", name="Підтвердити").click()

        assert "create" in post_calls, post_calls
        assert "restore" in post_calls, post_calls
        assert errors == [], f"pageerror: {errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        browser.close()


def _mock_chrome_data(page):
    """i.0.0.0.28: generic list endpoints so header/sidebar chrome tests avoid page errors."""
    _mock_session(page)
    _mock_status(page)
    page.route("**/api/jobs", lambda route: route.fulfill(json=[]))
    page.route("**/api/workers", lambda route: route.fulfill(json=[]))
    page.route("**/api/characters", lambda route: route.fulfill(json=[]))
    page.route("**/api/workflows", lambda route: route.fulfill(json=[]))
    page.route("**/api/brands", lambda route: route.fulfill(json=[]))


def test_header_metadata_matches_navigation():
    """i.0.0.0.28: header title/subtitle follow the URL and sidebar active state is correct."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_chrome_data(page)

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='header-title']")).to_have_text("Дашборд")
        expect(page.locator("[data-testid='header-subtitle']")).to_have_text("Огляд системи Vertep")
        # Sidebar active link highlights the dashboard.
        expect(page.locator("[data-testid='sidebar-nav'] a[aria-current='page']").first).to_have_text("Дашборд")

        page.goto(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='header-title']")).to_have_text("Завдання")
        expect(page.locator("[data-testid='sidebar-nav'] a[aria-current='page']").first).to_have_text("Завдання")

        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='header-title']")).to_have_text("Вузли")
        page.goto(f"{BASE_URL}/characters")
        expect(page.locator("[data-testid='header-title']")).to_have_text("Персонажі")

        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='header-title']")).to_have_text("Журнали")

        assert errors == [], f"pageerror: {errors}"
        browser.close()


def test_localization_scan_no_raw_english_chrome():
    """i.0.0.0.28: sidebar/header chrome carries no stray English UI labels."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _mock_chrome_data(page)
        page.goto(f"{BASE_URL}/")

        nav_text = page.locator("[data-testid='sidebar-nav']").inner_text()
        chrome_text = (
            page.locator("[data-testid='header-title']").inner_text()
            + " "
            + page.locator("[data-testid='header-subtitle']").inner_text()
        )
        forbidden = ["Workers", "Timeline", "Task Type", "Queue View", "Status Bar"]
        found = [term for term in forbidden if term in nav_text or term in chrome_text]
        assert found == [], f"raw English UI labels found in chrome: {found}"
        browser.close()


def test_collapsed_sidebar_icons_tooltips_no_overflow():
    """i.0.0.0.28: collapsed sidebar exposes icons with tooltips and no page overflow."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _mock_chrome_data(page)
        page.goto(f"{BASE_URL}/")

        page.locator("button[aria-label='Перемикач меню']").click()
        nav = page.locator("[data-testid='sidebar-nav']")
        expect(nav).to_be_visible()
        # First sidebar link (Дашборд) keeps an icon and exposes its label as a tooltip.
        link = nav.locator("a").first
        expect(link).to_have_attribute("title", "Дашборд")

        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"horizontal overflow in collapsed layout: {overflow}px"
        assert errors == [], f"pageerror: {errors}"
        browser.close()


def test_loading_state_resolves_to_content_or_error():
    """i.0.0.0.28: loading state does not hang forever — it resolves to data or error."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/jobs", lambda route: route.fulfill(json=[]))
        page.route("**/api/workers", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/")

        # Dashboard must reach a terminal state — loading must not hang forever.
        page.wait_for_timeout(2000)
        expect(page.locator("[data-testid='loading-state']")).not_to_be_visible()
        terminal_states = page.locator(
            "[data-testid='empty-state'], [data-testid='error-state'], [data-testid='workers-table-section'], [data-testid='stat-workers']"
        )
        expect(terminal_states.first).to_be_visible()
        browser.close()
