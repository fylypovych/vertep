#!/usr/bin/env python3
"""Browser E2E smoke tests for Vertep Web UI V2."""
import json
import os
import re
import socket
import sys
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright, expect
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
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
    """Імітувати сесію та спільні GET-запити сторінок.

    Окремі сценарії реєструють свої routes пізніше й перевизначають ці дані.
    Мутації та невідомі endpoints не підміняються успішною відповіддю.
    """
    _mock_status(page)
    for endpoint in ("jobs", "workers", "characters", "brands", "workflows", "channels/types"):
        page.route(f"**/api/{endpoint}", lambda route: (
            route.fulfill(json=[]) if route.request.method == "GET" else route.fallback()
        ))
    page.route("**/api/brands/*/channels", lambda route: (
        route.fulfill(json=[]) if route.request.method == "GET" else route.fallback()
    ))
    page.route("**/api/session", lambda route: route.fulfill(json={
        "authenticated": True, "user": "ci", "role": role,
    }))


def _attach_error_collector(page):
    """Shared pageerror + critical console-error collector for all critical tests.

    Returns ``(page_errors, console_errors)`` lists.  Every critical browser
    test should call this to avoid false-green results from swallowed errors.
    """
    page_errors = []
    console_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    def collect_console_error(msg):
        # Chromium вважає HTTP 4xx/5xx console errors. Їх перевіряють через
        # явний error state сторінки; цей колектор відстежує runtime-помилки JS.
        if msg.type == "error" and not msg.text.startswith("Failed to load resource:"):
            console_errors.append(msg.text)

    page.on("console", collect_console_error)
    return page_errors, console_errors


def _assert_no_js_errors(page_errors, console_errors):
    """Assert both collected error lists are empty."""
    assert page_errors == [], f"pageerror: {page_errors}"
    assert console_errors == [], f"console.error: {console_errors}"


def test_setup_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        page.route("**/api/setup", lambda route: route.fulfill(json={
            "configured": False, "selected_role": None, "hardware": {},
            "roles": {"core": {"label": "Основний сервер", "modules": [], "capabilities": []}},
        }))
        page.goto(f"{BASE_URL}/setup?token=ci")
        assert "Vertep" in page.title()
        expect(page.locator("body")).to_contain_text("Перший запуск")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_dashboard_loads_and_navigation_works_without_javascript_errors():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        page.route("**/api/workers*", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))

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

        page.locator("nav a[href='/settings?tab=system']").click()
        expect(page).to_have_url(f"{BASE_URL}/settings?tab=system")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        expect(page.locator("[data-testid='backends-table']")).to_be_visible()

        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_character_create_and_edit_use_localized_form():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        saved = []
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_worker_wizard_role_labels_are_ukrainian():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()

        page.locator("[data-testid='create-worker-button']").click()
        expect(page.locator("[data-testid='worker-wizard-modal']")).to_be_visible()
        expect(page.locator("[data-testid='worker-wizard-modal'] h3")).to_have_text("Додати вузол")
        expect(page.locator("[data-testid='worker-role-select']")).to_have_value("gpu")
        expect(page.locator("[data-testid='worker-role-select'] option")).to_have_count(6)
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_jobs_list_shows_empty_state_and_create_form():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='jobs-page']")).to_be_visible()
        expect(page.locator("[data-testid='jobs-empty']")).to_contain_text("Завдань не знайдено")
        expect(page.locator("[data-testid='create-job-button']")).to_be_visible()

        page.locator("[data-testid='create-job-button']").click()
        expect(page.locator("[data-testid='create-job-modal']")).to_be_visible()
        expect(page.locator("[data-testid='job-topic-input']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_job_detail_view_and_edit():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[job]))
        page.goto(f"{BASE_URL}/jobs/{job_id}")

        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("text=Тестове завдання")).to_be_visible()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Готове")
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("07.09.2026")

        page.locator("[data-testid='edit-job-button']").click()
        expect(page.locator("[data-testid='edit-topic-input']")).to_be_visible()

        page.get_by_role("button", name="Скасувати").click()
        expect(page.locator("[data-testid='edit-topic-input']")).not_to_be_visible()

        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_jobs_delete_button_is_present():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)

        jobs = [{"job_id": "job-1", "topic": "Test", "status": "READY",
                 "created_at": "2026-09-07T12:00:00Z", "priority": 5, "character_id": "c1"}]

        page.route("**/api/jobs**", lambda route: route.fulfill(json={"items": jobs, "total": 1, "pages": 1}))
        page.route("**/api/characters**", lambda route: route.fulfill(json=[]))
        page.route("**/api/brands**", lambda route: route.fulfill(json=[]))
        page.route("**/api/workflows**", lambda route: route.fulfill(json=[]))
        page.route("**/api/tasks/queue**", lambda route: route.fulfill(json={"ready": [], "inflight": []}))
        page.route("**/api/tasks/dead-letter**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs")
        expect(page.locator("[data-testid='jobs-page']")).to_be_visible()
        expect(page.locator("button:has-text('Видалити')").first).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_dashboard_job_status_counts():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        page.route("**/api/workers*", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_shows_user_friendly_system_info():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_security_shows_effective_checks_and_remediation():
    """Issue #84: the security section must render the effective
    secret-store/certificate/integration state, not only an OK flag."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {},
            "update": {"current_version": "0.0.1.19", "state": "IDLE"},
        }))
        page.route("**/api/security/check", lambda route: route.fulfill(json={
            "ok": False,
            "weak_or_missing": ["ADMIN_PASSWORD"],
            "recommendation": "The sealed secret-store data key cannot be opened with the configured passphrase",
            "checks": {
                "secrets_store": {
                    "status": "unusable", "sealed": True,
                    "detail": "sealed but not openable: secret-store key failed authentication",
                    "unsealable": False, "passphrase_configured": True, "sealing_required": True,
                },
                "certificates": {
                    "server_certificate": {"present": True, "status": "expiring",
                                           "sha256": "a" * 64, "expires_at": "2026-10-01T00:00:00+00:00",
                                           "days_remaining": 2, "subject": "CN=vertep"},
                    "server_key": {"present": True, "status": "ok", "sha256": "b" * 64},
                    "node_ca": {"present": False, "status": "missing"},
                },
                "integrations": [{"name": "publisher:telegram", "status": "not_configured"}],
            },
        }))
        page.route("**/api/system/certificates*", lambda route: route.fulfill(json={"certificates": []}))

        page.goto(f"{BASE_URL}/settings?tab=security")
        section = page.locator("[data-testid='settings-security']")
        expect(section).to_be_visible()
        expect(page.locator("[data-testid='security-check-state']")).to_have_text("Потребує уваги")
        expect(page.locator("[data-testid='security-check-weak']")).to_contain_text("ADMIN_PASSWORD")
        expect(page.locator("[data-testid='security-check-recommendation']")).to_contain_text("cannot be opened")
        expect(page.locator("[data-testid='security-check-secret-store']")).to_contain_text("не працює")
        expect(page.locator("[data-testid='security-cert-server_certificate']")).to_contain_text("спливає")
        expect(page.locator("[data-testid='security-cert-node_ca']")).to_contain_text("відсутній")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


# ---------------------------------------------------------------------------
# Issue #122 P8 — Settings → Движок відео
# ---------------------------------------------------------------------------

VIDEO_ENGINE_NATIVE = {
    "selected": "native", "effective": "native", "agree": True,
    "label": "Vertep Native", "config_revision": "sha256:" + "a" * 64,
    "endpoint": None, "secret": None, "fields": [], "upstream_reference": None,
    "ready": True, "reason": "native_engine", "probed": False,
    "options": [{"id": "native", "label": "Vertep Native"},
                {"id": "money-printer", "label": "MoneyPrinterTurbo"}],
    "required_fields": {
        "native": [],
        "money-printer": [
            {"name": "endpoint", "env": "MONEY_PRINTER_URL", "value": None,
             "configured": False, "secret": False},
            {"name": "token", "env": "MONEY_PRINTER_TOKEN", "value": None,
             "configured": False, "secret": True},
        ],
    },
    "system_state": "NORMAL", "change_allowed": True, "values_exposed": False,
}


def _video_engine_state(backend: str, ready: bool, reason: str, endpoint=None):
    """A read model of the engine that is currently effective."""
    selected = backend if backend != "native" else "money-printer"
    fields = []
    if backend != "native":
        fields = [
            {"name": "endpoint", "env": "MONEY_PRINTER_URL", "value": endpoint,
             "configured": bool(endpoint), "secret": False},
            {"name": "token", "env": "MONEY_PRINTER_TOKEN", "value": None,
             "configured": False, "secret": True},
        ]
    return {
        **VIDEO_ENGINE_NATIVE,
        "selected": selected,
        "effective": backend,
        "agree": True,
        "label": "Vertep Native" if backend == "native" else "MoneyPrinterTurbo",
        "ready": ready,
        "reason": reason,
        "fields": fields,
        "endpoint": endpoint,
        "probed": backend != "native",
    }


def test_settings_video_engine_names_both_engines_and_shows_the_effective_one():
    """Both names are rendered, and the displayed choice is the one the API reports."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/settings/video-engine*",
                   lambda route: route.fulfill(json=VIDEO_ENGINE_NATIVE))

        page.goto(f"{BASE_URL}/settings?tab=video-engine")

        section = page.locator("[data-testid='settings-video-engine']")
        expect(section).to_be_visible()
        options = page.locator("[data-testid='video-engine-select'] option")
        expect(options).to_have_count(2)
        expect(options.nth(0)).to_have_text("Vertep Native")
        expect(options.nth(1)).to_have_text("MoneyPrinterTurbo")
        expect(page.locator("[data-testid='video-engine-selected']")).to_have_text("Vertep Native")
        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text("Vertep Native")
        expect(page.locator("[data-testid='video-engine-revision']")).to_contain_text("sha256:")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_video_engine_applies_an_engine_through_the_real_api():
    """Browser → POST → read model: the applied engine is the effective engine.

    The mutation is answered by the route handler the real API would answer, and the
    answer the UI renders afterwards is the one the read model reports.
    """
    applied = {}
    state = {"config": VIDEO_ENGINE_NATIVE}

    def _switch(route):
        payload = route.request.post_data_json or {}
        applied.update(payload)
        state["config"] = _video_engine_state(
            payload.get("backend", "native"), True, "ok", endpoint=payload.get("endpoint"))
        route.fulfill(json={
            "slot": "video_engine", "backend": payload.get("backend"), "changed": True,
            "env": "VERTEP_VIDEO_ENGINE", "matrix": {},
            "effective_engine": state["config"],
        })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/settings/video-engine*",
                   lambda route: route.fulfill(json=state["config"]))
        page.route("**/api/settings/providers/video_engine", _switch)

        page.goto(f"{BASE_URL}/settings?tab=video-engine")
        expect(page.locator("[data-testid='video-engine-panel']")).to_be_visible()

        # Select the external engine, give its runtime address, apply.
        page.select_option("[data-testid='video-engine-select']", "money-printer")
        expect(page.locator("[data-testid='video-engine-endpoint']")).to_be_visible()
        page.fill("[data-testid='video-engine-endpoint']", "http://runtime:8098")
        page.click("[data-testid='video-engine-apply']")

        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text("MoneyPrinterTurbo")
        assert applied["backend"] == "money-printer"
        assert applied["endpoint"] == "http://runtime:8098", \
            "the endpoint must be applied together with the engine"
        expect(page.locator("[data-testid='video-engine-token-state']")).to_contain_text(
            "MONEY_PRINTER_TOKEN")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_video_engine_reports_a_refused_apply_and_stays_on_the_previous_engine():
    """A runtime that cannot prove readiness must not appear as applied."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/settings/video-engine*",
                   lambda route: route.fulfill(json=VIDEO_ENGINE_NATIVE))
        page.route("**/api/settings/providers/video_engine", lambda route: route.fulfill(
            status=409,
            json={"detail": "Engine 'money-printer' runtime is not ready: upstream_unreachable"},
        ))

        page.goto(f"{BASE_URL}/settings?tab=video-engine")
        expect(page.locator("[data-testid='video-engine-panel']")).to_be_visible()
        page.select_option("[data-testid='video-engine-select']", "money-printer")
        page.click("[data-testid='video-engine-apply']")

        expect(page.locator("[data-testid='video-engine-error']")).to_contain_text("not ready")
        expect(page.locator("[data-testid='video-engine-rollback']")).to_contain_text(
            "Попередній движок збережено")
        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text("Vertep Native")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_video_engine_returns_to_native():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        current = {"config": _video_engine_state("money-printer", True, "ok",
                                                 endpoint="http://runtime:8098")}
        page.route("**/api/settings/video-engine*",
                   lambda route: route.fulfill(json=current["config"]))

        def _switch(route):
            payload = route.request.post_data_json or {}
            current["config"] = _video_engine_state(
                payload.get("backend", "native"), True, "native_engine")
            route.fulfill(json={
                "slot": "video_engine", "backend": payload.get("backend"), "changed": True,
                "env": "VERTEP_VIDEO_ENGINE", "matrix": {},
                "effective_engine": current["config"],
            })

        page.route("**/api/settings/providers/video_engine", _switch)

        page.goto(f"{BASE_URL}/settings?tab=video-engine")
        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text("MoneyPrinterTurbo")
        page.click("[data-testid='video-engine-return-native']")

        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text("Vertep Native")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def _external_engine_state(endpoint):
    """MoneyPrinterTurbo as the effective engine, with one runtime address.

    The read model reports the same inputs both for the effective engine and in the
    per-engine list Settings renders them from, so the address is written in both places.
    """
    state = _video_engine_state("money-printer", True, "ok", endpoint=endpoint)
    state["required_fields"] = {**VIDEO_ENGINE_NATIVE["required_fields"],
                                "money-printer": state["fields"]}
    return state


def test_settings_video_engine_repoints_the_runtime_of_the_current_engine():
    """P8: адресу runtime можна змінити, не перемикаючи сам движок.

    Кнопка «Застосувати» була прив'язана лише до імені движка, тому вже ефективний
    MoneyPrinterTurbo не мож було перевести на інший адрес runtime узагалі. Тепер Apply
    стежить і за адресою, і POST несе нову адресу разом із тим самим backend.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        current = {"config": _external_engine_state("http://runtime:8098")}
        applied: dict = {}
        page.route("**/api/settings/video-engine*",
                   lambda route: route.fulfill(json=current["config"]))

        def _switch(route):
            payload = route.request.post_data_json or {}
            applied.update(payload)
            current["config"] = _external_engine_state(payload.get("endpoint"))
            route.fulfill(json={
                "slot": "video_engine", "backend": payload.get("backend"), "changed": True,
                "env": "VERTEP_VIDEO_ENGINE", "matrix": {},
                "effective_engine": current["config"],
            })

        page.route("**/api/settings/providers/video_engine", _switch)

        page.goto(f"{BASE_URL}/settings?tab=video-engine")
        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text(
            "MoneyPrinterTurbo")
        # Нічого не змінено — застосовувати нема чого.
        expect(page.locator("[data-testid='video-engine-apply']")).to_be_disabled()

        page.fill("[data-testid='video-engine-endpoint']", "http://runtime:9090")
        expect(page.locator("[data-testid='video-engine-apply']")).to_be_enabled()
        # Застосування супроводжується повторним читанням конфігурації: дочекатися
        # цієї відповіді, інакше наступна правка адреси змагалася б із нею.
        with page.expect_response(
                lambda response: "/api/settings/video-engine" in response.url
                and response.request.method == "GET") as reload_response:
            page.click("[data-testid='video-engine-apply']")
        assert reload_response.value.status == 200, "повторне читання конфігурації движка"

        assert applied["backend"] == "money-printer", "движок не змінюється — змінюється адреса"
        assert applied["endpoint"] == "http://runtime:9090"
        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text(
            "MoneyPrinterTurbo")
        expect(page.locator("[data-testid='video-engine-endpoint']")).to_have_value(
            "http://runtime:9090")
        expect(page.locator("[data-testid='video-engine-apply']")).to_be_disabled(), \
            "форма знову описує те, що вже ефективно"

        # Повернення попередньої адреси — знову реальна зміна ефективної конфігурації,
        # тому Apply має лишатися доступним: ефективний runtime тепер 9090.
        page.fill("[data-testid='video-engine-endpoint']", "http://runtime:8098")
        expect(page.locator("[data-testid='video-engine-apply']")).to_be_enabled()
        assert applied["endpoint"] == "http://runtime:9090", \
            "зміна адреси без застосування нічого не перезаписує"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_video_engine_is_locked_while_the_system_state_forbids_changes():
    """A state that forbids configuration must not offer the change at all."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/settings/video-engine*", lambda route: route.fulfill(json={
            **VIDEO_ENGINE_NATIVE, "system_state": "UPDATING", "change_allowed": False,
        }))

        page.goto(f"{BASE_URL}/settings?tab=video-engine")

        expect(page.locator("[data-testid='video-engine-system-state']")).to_have_text("UPDATING")
        expect(page.locator("[data-testid='video-engine-locked']")).to_contain_text("UPDATING")
        expect(page.locator("[data-testid='video-engine-apply']")).to_be_disabled()
        expect(page.locator("[data-testid='video-engine-select']")).to_be_disabled()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_video_engine_shows_a_mismatch_instead_of_hiding_it():
    """A selection that is not effective must be visible, never silently replaced."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/settings/video-engine*", lambda route: route.fulfill(json={
            **VIDEO_ENGINE_NATIVE, "selected": "money-printer", "agree": False,
            "ready": False, "reason": "endpoint_missing",
        }))

        page.goto(f"{BASE_URL}/settings?tab=video-engine")

        expect(page.locator("[data-testid='video-engine-selected']")).to_have_text("MoneyPrinterTurbo")
        expect(page.locator("[data-testid='video-engine-effective']")).to_have_text("Vertep Native")
        expect(page.locator("[data-testid='video-engine-mismatch']")).to_be_visible()
        expect(page.locator("[data-testid='video-engine-reason']")).to_contain_text("адресу")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_security_renders_ok_state():
    """Issue #84: a healthy installation must render the localized 'ok' state and
    still show the effective detail rows."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/status", lambda route: route.fulfill(json={
            "core": "OK", "version": "0.0.1.19",
            "system": {"state": "NORMAL"},
            "queue": {"depth": 0, "inflight": 0, "dead_letter": 0},
            "orchestration": {"active_jobs": 0, "active_scenes": 0},
            "providers": {},
            "update": {"current_version": "0.0.1.19", "state": "IDLE"},
        }))
        page.route("**/api/security/check", lambda route: route.fulfill(json={
            "ok": True,
            "weak_or_missing": [],
            "recommendation": "Environment credentials and certificates are within policy",
            "checks": {
                "secrets_store": {
                    "status": "ok", "sealed": True,
                    "detail": "sealed (data key wrapped with passphrase-derived KEK)",
                    "unsealable": True, "passphrase_configured": True, "sealing_required": True,
                },
                "certificates": {
                    "server_certificate": {"present": True, "status": "ok", "sha256": "c" * 64},
                    "server_key": {"present": True, "status": "ok", "sha256": "d" * 64},
                    "node_ca": {"present": True, "status": "ok", "sha256": "e" * 64},
                },
                "integrations": [],
            },
        }))
        page.route("**/api/system/certificates*", lambda route: route.fulfill(json={"certificates": []}))

        page.goto(f"{BASE_URL}/settings?tab=security")
        section = page.locator("[data-testid='settings-security']")
        expect(section).to_be_visible()
        expect(page.locator("[data-testid='security-check-state']")).to_have_text("OK")
        expect(page.locator("[data-testid='security-check-secret-store']")).to_contain_text("у нормі")
        expect(page.locator("[data-testid='security-cert-server_certificate']")).to_contain_text("у нормі")
        expect(section).to_contain_text("Немає активних інтеграцій")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_shows_resources_or_unavailable():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        page.route("**/api/workers*", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='resources']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_update_shows_correct_version():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_dashboard_architecture_shows_core_and_workers():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        page.route("**/api/workers*", lambda route: route.fulfill(json=[
            {"node_id": "w1", "node_name": "GPU-Node-1", "role": "gpu", "status": "ONLINE",
             "capabilities": ["image_generation", "video_generation"]},
            {"node_id": "w2", "node_name": "Text-Node-1", "role": "text", "status": "ONLINE",
             "capabilities": ["llm"]},
        ]))
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='architecture']")).to_be_visible()
        expect(page.locator("[data-testid='architecture']")).to_contain_text("CORE")
        expect(page.locator("[data-testid='architecture']")).to_contain_text("GPU")
        expect(page.locator("[data-testid='architecture']")).to_contain_text("Текст")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_logs_page_loads_empty():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='logs-page']")).to_be_visible()
        expect(page.locator("[data-testid='empty-state']")).to_contain_text("Логів не знайдено")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_logs_page_shows_entries():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(json=[
            {"level": "INFO", "message": "Core started", "timestamp": "2026-09-08T10:00:00Z", "node_name": "core"},
            {"level": "ERROR", "message": "Worker failed", "timestamp": "2026-09-08T10:01:00Z", "node_name": "gpu-1", "job_id": "j-001"},
        ]))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='logs-table']")).to_be_visible()
        expect(page.locator("[data-testid='logs-table']")).to_contain_text("Core started")
        expect(page.locator("[data-testid='logs-table']")).to_contain_text("Worker failed")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_logs_page_shows_api_error():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(status=500, json={"detail": "Internal error"}))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='error-state']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_logs_page_filter_level():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/logs*", lambda route: route.fulfill(json=[
            {"level": "ERROR", "message": "Only error", "timestamp": "2026-09-08T10:00:00Z"},
        ]))
        page.goto(f"{BASE_URL}/logs")
        expect(page.locator("[data-testid='logs-table']")).to_be_visible()
        expect(page.locator("[data-testid='logs-table']")).to_contain_text("ERROR")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_health_endpoint_reports_core():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        response = page.request.get(f"{BASE_URL}/api/health")
        assert response.ok
        assert response.json()["service"] == "core"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()

def test_job_detail_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_queue_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/tasks/queue**", lambda route: route.fulfill(json={
            "ready": [], "inflight": [],
        }))
        page.route("**/api/tasks/dead-letter**", lambda route: route.fulfill(json=[]))
        page.route("**/api/jobs**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/queue")
        expect(page.locator("[data-testid='queue-page']")).to_be_visible()
        expect(page.locator("[data-testid='queue-page']")).to_contain_text("Виконання завдань")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_alerts_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/alerts**", lambda route: route.fulfill(json=[
            {"severity": "error", "type": "JOB_FAILED", "message": "Test failure", "job_id": "j-001"},
        ]))
        page.goto(f"{BASE_URL}/alerts")
        expect(page.locator("[data-testid='alerts-page']")).to_be_visible()
        expect(page.locator("[data-testid='alerts-page']")).to_contain_text("JOB_FAILED")
        expect(page.locator("[data-testid='alerts-page']")).to_contain_text("Test failure")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_health_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_published_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/jobs**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/published")
        expect(page.locator("[data-testid='published-page']")).to_be_visible()
        expect(page.locator("[data-testid='published-page']")).to_contain_text("Опубліковані матеріали")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()



def test_workers_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/workers**", lambda route: route.fulfill(json=[
            {"node_id": "n1", "node_name": "GPU-Node-1", "role": "gpu", "status": "ONLINE",
             "capabilities": ["image_generation"], "vram_mb": 8192, "gpu_name": "RTX 4090"},
        ]))
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()
        expect(page.locator("[data-testid='workers-table']")).to_contain_text("GPU-Node-1")
        expect(page.locator("[data-testid='create-worker-button']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_worker_detail_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_characters_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/characters**", lambda route: route.fulfill(json=[
            {"id": "char1", "name": "Дід Самогон", "language": "uk", "enabled": True,
             "system_prompt": "Test", "voice": {}, "visual": {}, "generation": {}, "publishing": {}},
        ]))
        page.goto(f"{BASE_URL}/characters")
        expect(page.locator("[data-testid='characters-page']")).to_be_visible()
        expect(page.locator("[data-testid='characters-page']")).to_contain_text("Дід Самогон")
        expect(page.locator("[data-testid='create-character-button']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/workflows**", lambda route: route.fulfill(json=[
            {"kind": "image", "name": "demo.json"},
        ]))
        page.route("**/api/characters**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/workflows")
        expect(page.locator("[data-testid='workflows-page']")).to_be_visible()
        expect(page.locator("[data-testid='workflows-table']")).to_contain_text("demo.json")
        expect(page.locator("[data-testid='create-workflow-button']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


# Реальна форма payload, яку повертає WorkflowRegistry.list() — містить `type`,
# а не `kind`. Мокати `kind` тут не можна: це маскувало б розбіжність контракту.
WORKFLOW_LIST_ITEM = {
    "type": "image",
    "name": "demo.json",
    "path": "workflows/image/demo.json",
    "valid": True,
    "errors": [],
    "warnings": [],
    "schema": {
        "node_count": 1,
        "node_types": ["KSampler"],
        "has_placeholders": ["TOPIC"],
        "estimated_vram_mb": 0,
    },
}

WORKFLOW_DOCUMENT = {
    "workflow": {"n1": {"class_type": "KSampler", "inputs": {"seed": 42, "model": "sd.json"}}},
    "validation": {
        "valid": True,
        "errors": [],
        "warnings": [],
        "schema": {
            "node_count": 1,
            "node_types": ["KSampler"],
            "has_placeholders": [],
            "estimated_vram_mb": 0,
        },
    },
    "meta": {"updated_at": "2026-09-30T00:00:00+00:00"},
}

WORKFLOW_FORM_SCHEMA = {
    "type": "image",
    "kind": "image",
    "name": "demo.json",
    "validation": WORKFLOW_DOCUMENT["validation"],
    "form_fields": [
        {"node_id": "n1", "input_name": "seed", "current_value": 42, "type": "int", "class_type": "KSampler"},
        {"node_id": "n1", "input_name": "model", "current_value": "sd.json", "type": "str", "class_type": "KSampler"},
    ],
    "schema": {
        "placeholders": [],
        "node_types": ["KSampler"],
        "node_count": 1,
        "editable_inputs": [
            {"node_id": "n1", "input_name": "seed", "current_value": 42, "type": "int", "class_type": "KSampler"},
            {"node_id": "n1", "input_name": "model", "current_value": "sd.json", "type": "str", "class_type": "KSampler"},
        ],
    },
}

WORKFLOW_USAGE = {
    "workflow": "workflows/image/demo.json",
    "jobs": [{"job_id": "2026-000001", "topic": "Usage", "status": "SCRIPT_QUEUED"}],
    "characters": [{"character_id": "narrator", "field": "generation"}],
    "total_jobs": 1,
    "total_characters": 1,
}


def _mock_workflow_registry(page, list_items=None, usage=None, versions=None, capture=None, put_status=200, put_json=None):
    """Мокує workflow registry реальними формами payload від WorkflowRegistry."""
    items = list_items if list_items is not None else [WORKFLOW_LIST_ITEM]
    usage_payload = usage if usage is not None else WORKFLOW_USAGE
    versions_payload = versions if versions is not None else [
        {"version": 1, "archived_at": "2026-09-29T10:00:00+00:00", "deleted": False, "size_bytes": 128},
    ]

    def handler(route):
        url = route.request.url
        method = route.request.method
        if method == "POST" and "/api/workflows/validate" in url:
            body = route.request.post_data or "{}"
            try:
                payload = json.loads(body)
            except ValueError:
                return route.fulfill(json={
                    "valid": False, "errors": ["Невалідний формат JSON"], "warnings": [],
                    "schema": {"node_count": 0, "node_types": [], "has_placeholders": []},
                })
            if not isinstance(payload, dict) or not payload:
                errors = ["Workflow must be a non-empty object"]
            else:
                errors = [
                    f"Node {node_id} has no class_type"
                    for node_id, node in payload.items()
                    if not isinstance(node, dict) or not node.get("class_type")
                ]
            return route.fulfill(json={
                "valid": not errors,
                "errors": errors,
                "warnings": [],
                "schema": {
                    "node_count": len(payload) if isinstance(payload, dict) else 0,
                    "node_types": ["KSampler"],
                    "has_placeholders": ["TOPIC"],
                    "estimated_vram_mb": 0,
                },
            })
        if method == "DELETE":
            if "force=true" in url:
                return route.fulfill(json={"deleted": "workflows/image/demo.json", "dependencies": None, "archived": True})
            return route.fulfill(
                status=409,
                json={"detail": {
                    "message": "Робочий процес використовується у: персонаж narrator",
                    "error_code": "WORKFLOW_IN_USE",
                    "usage": {"jobs": usage_payload["jobs"], "characters": usage_payload["characters"]},
                }},
            )
        if method == "PUT":
            if capture is not None:
                capture.append({
                    "url": url,
                    "body": json.loads(route.request.post_data or "{}"),
                })
            if put_status != 200:
                return route.fulfill(status=put_status, json=put_json or {"detail": "Workflow already exists and is valid. Use force=true to overwrite."})
            return route.fulfill(json=put_json or {"type": "image", "name": "demo.json", "valid": True, "validation": WORKFLOW_DOCUMENT["validation"]})
        if url.endswith("/form"):
            return route.fulfill(json=WORKFLOW_FORM_SCHEMA)
        if url.endswith("/usage"):
            return route.fulfill(json=usage_payload)
        if "/versions/" in url:
            return route.fulfill(json=versions_payload)
        if url.endswith("/versions"):
            return route.fulfill(json=versions_payload)
        if url.rstrip("/").endswith("/api/workflows/image/demo.json"):
            return route.fulfill(json=WORKFLOW_DOCUMENT)
        return route.fulfill(json=items)

    page.route("**/api/workflows**", handler)


def test_workflows_list_renders_type_from_backend_payload():
    """Backend віддає `type`, Angular має показати колонку «Тип» і usage graph."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page)
        page.goto(f"{BASE_URL}/workflows")
        expect(page.locator("[data-testid='workflows-page']")).to_be_visible()
        table = page.locator("[data-testid='workflows-table']")
        expect(table).to_contain_text("demo.json")
        expect(table.locator("tbody tr").first.locator("td").first).to_have_text("image")
        expect(page.locator("[data-testid='workflow-validation-status']").first).to_have_text("Валідний")
        expect(table).to_contain_text("персонаж: narrator")
        expect(table).to_contain_text("завдання: 2026-000001")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_editor_form_and_validation_report_tabs():
    """Form-вкладка має реальні editable_inputs, звіт — серверну валідацію."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page)
        page.goto(f"{BASE_URL}/workflows")
        page.get_by_role("button", name="Редагувати").first.click()
        editor = page.locator("[data-testid='workflow-editor-modal']")
        expect(editor).to_be_visible()
        expect(editor.locator("textarea")).to_have_value(re.compile("KSampler"))

        editor.get_by_role("button", name="Форма", exact=True).click()
        form_view = page.locator("[data-testid='workflow-form-view']")
        expect(form_view).to_be_visible()
        expect(form_view).to_contain_text("KSampler")
        expect(form_view).to_contain_text("seed")
        expect(form_view.locator("input")).to_have_count(2)

        editor.get_by_role("button", name="Звіт валідації", exact=True).click()
        report = page.locator("[data-testid='workflow-validation-report']")
        expect(report).to_be_visible()
        expect(report).to_contain_text("Сценарій валідний")
        expect(report).to_contain_text("KSampler")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_form_control_edits_json_and_persists_with_immutable_identity():
    """Типований контрол форми має оновлювати JSON і зберегатися з незмінною identity."""
    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page, capture=captured)
        page.goto(f"{BASE_URL}/workflows")
        page.get_by_role("button", name="Редагувати").first.click()
        editor = page.locator("[data-testid='workflow-editor-modal']")
        expect(editor).to_be_visible()

        editor.get_by_role("button", name="Форма", exact=True).click()
        form_view = page.locator("[data-testid='workflow-form-view']")
        expect(form_view).to_be_visible()
        form_view.locator("input").nth(0).fill("1234")
        form_view.locator("input").nth(1).fill("sdxl.json")

        editor.get_by_role("button", name="JSON", exact=True).click()
        expect(editor.locator("textarea")).to_have_value(re.compile(r'"seed": 1234'))
        expect(editor.locator("textarea")).to_have_value(re.compile(r'"model": "sdxl.json"'))

        editor.get_by_role("button", name="Зберегти").click()
        expect(page.locator("[data-testid='workflow-editor-modal']")).to_be_hidden()

        assert len(captured) == 1, f"expected exactly one PUT, got {captured}"
        assert captured[0]["url"].endswith("/api/workflows/image/demo.json?force=true")
        assert captured[0]["body"]["n1"]["inputs"]["seed"] == 1234
        assert captured[0]["body"]["n1"]["inputs"]["model"] == "sdxl.json"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_create_rejects_duplicate_name_without_force():
    """Створення з існуючим ім'ям не перезаписує файл мовчки (immutable create identity)."""
    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page, capture=captured, put_status=400)
        page.goto(f"{BASE_URL}/workflows")
        page.locator("[data-testid='create-workflow-button']").click()
        editor = page.locator("[data-testid='workflow-editor-modal']")
        expect(editor).to_be_visible()
        editor.locator("[data-testid='workflow-name-input']").fill("demo.json")
        editor.locator("textarea").fill('{"n1": {"class_type": "KSampler", "inputs": {"seed": 1}}}')
        editor.get_by_role("button", name="Зберегти").click()

        # Модалка лишається відкритою: create не перезаписує файл мовчки.
        expect(editor).to_be_visible()
        assert len(captured) == 1, f"expected exactly one PUT, got {captured}"
        assert captured[0]["url"].endswith("/api/workflows/image/demo.json"), captured[0]["url"]
        assert "force=true" not in captured[0]["url"], "create must not send force"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_editor_validation_updates_from_server():
    """Правка JSON має оновлювати звіт із серверного validate endpoint."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page)
        page.goto(f"{BASE_URL}/workflows")
        page.get_by_role("button", name="Редагувати").first.click()
        editor = page.locator("[data-testid='workflow-editor-modal']")
        expect(editor).to_be_visible()
        editor.get_by_role("button", name="Звіт валідації", exact=True).click()
        expect(page.locator("[data-testid='workflow-validation-report']")).to_contain_text("Сценарій валідний")

        editor.get_by_role("button", name="JSON", exact=True).click()
        editor.locator("textarea").fill('{"n1": {"inputs": {}}}')
        editor.get_by_role("button", name="Звіт валідації", exact=True).click()
        report = page.locator("[data-testid='workflow-validation-report']")
        expect(report).to_contain_text("Сценарій містить помилки")
        expect(report).to_contain_text("no class_type")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_version_history_and_restore():
    """Модалка історії показує версії та відновлює обрану."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page)
        page.goto(f"{BASE_URL}/workflows")
        page.get_by_role("button", name="Історія").first.click()
        modal = page.locator("[data-testid='workflow-versions-modal']")
        expect(modal).to_be_visible()
        expect(modal).to_contain_text("Версія 1")
        modal.get_by_role("button", name="Відновити").click()
        expect(modal).to_be_hidden()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_workflows_delete_conflict_shows_structured_usage():
    """409 має відкривати модалку з usage і підтримувати force-видалення."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_workflow_registry(page)
        page.goto(f"{BASE_URL}/workflows")
        page.locator("[data-testid='workflows-table']").get_by_role("button", name="Видалити").first.click()
        confirm_dialog = page.get_by_role("dialog", name="Видалити сценарій")
        expect(confirm_dialog).to_be_visible()
        confirm_dialog.get_by_role("button", name="Підтвердити").click()

        conflict = page.locator("[data-testid='workflow-conflict-modal']")
        expect(conflict).to_be_visible()
        expect(conflict).to_contain_text("Конфлікт видалення (409)")
        expect(conflict).to_contain_text("narrator")
        expect(conflict).to_contain_text("2026-000001")
        conflict.get_by_role("button", name="Видалити примусово").click()
        expect(conflict).to_be_hidden()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_brands_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/brands**", lambda route: route.fulfill(json=[
            {"id": "brand1", "name": "Test Brand", "enabled": True, "metadata": {}, "publishing": {}},
        ]))
        page.route("**/api/brands/*/channels**", lambda route: route.fulfill(json=[]))
        page.route("**/api/characters**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/brands")
        expect(page.locator("[data-testid='brands-page']")).to_be_visible()
        expect(page.locator("[data-testid='brands-page']")).to_contain_text("Test Brand")
        expect(page.locator("[data-testid='create-brand-button']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_worker_wizard_opens_and_shows_token():
    """V2C-401: Worker onboarding wizard generates token with TTL."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        page.route("**/api/workers**", lambda route: route.fulfill(json=[]))
        page.route("**/api/nodes**", lambda route: route.fulfill(json=[]))
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_worker_detail_shows_hardware_and_actions():
    """V2C-402/V2C-403: Worker detail shows typed hardware and action buttons."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_settings_roles_shows_deployment_status():
    """V2C-404: Settings roles section shows deployment state."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page, {"update": {"current_version": "0.0.1.99", "state": "IDLE"}})
        page.route("**/api/system/roles", lambda route: route.fulfill(json={
            "node_role": "core", "active_roles": [],
            "role_runtime_status": {"core": "READY", "gpu": "DEGRADED", "text": "OFFLINE"},
            "available_roles": [
                {"id": "gpu", "label": "GPU Node", "services": ["comfyui"],
                 "capabilities": ["image_generation"], "runtime_status": "DEGRADED",
                 "runtime_evidence": {"source": "nodes",
                                       "nodes": [{"node_id": "gpu-01", "runtime_status": "OFFLINE",
                                                  "heartbeat": "OFFLINE", "live": False}]}},
                {"id": "text", "label": "Text Node", "services": ["ollama"],
                 "capabilities": ["text_generation"], "runtime_status": "OFFLINE",
                 "runtime_evidence": {"source": "nodes", "nodes": []}},
            ],
            "deployment": {"state": None, "error": None},
            "queued": False,
        }))
        # Catch-all for other settings API calls
        for pattern in ["**/api/settings/secrets**", "**/api/system/secrets**", "**/api/secrets**",
                        "**/api/models/**", "**/api/system/update/**",
                        "**/api/system/backups**", "**/api/system/settings/**", "**/api/security/**",
                        "**/api/status**", "**/api/integrations**", "**/api/system/certificates**",
                        "**/api/system/license**", "**/api/system/installation-manifest**"]:
            page.route(pattern, lambda route: route.fulfill(json={}))
        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_be_visible()
        page.locator("a[href='/settings?tab=roles']").click()
        expect(page).to_have_url(f"{BASE_URL}/settings?tab=roles")
        expect(page.locator("[data-testid='roles-save-button']")).to_be_visible()
        # Measured runtime status per role contract must be rendered, not just declared.
        expect(page.locator("[data-testid='role-runtime-status-list']")).to_be_visible()
        expect(page.locator("[data-testid='role-status-core']")).to_have_text("READY")
        expect(page.locator("[data-testid='role-status-gpu']")).to_have_text("DEGRADED")
        expect(page.locator("[data-testid='role-status-text']")).to_have_text("OFFLINE")
        page.locator("nav a[href='/settings?tab=system']").click()
        expect(page.locator("[data-testid='roles-save-button']")).not_to_be_visible()
        page.go_back()
        expect(page.locator("[data-testid='roles-save-button']")).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()
def _script_job(job_id="job-script-01", status="SCRIPT_PENDING_APPROVAL"):
    """Minimal Job in script review state (i.0.0.0.4)."""
    return {
        "job_id": job_id, "topic": "Сценарій до затвердження", "character_id": "did_samogon",
        "priority": 5, "status": status, "created_at": "2026-09-08T10:00:00Z",
        "updated_at": "2026-09-08T10:05:00Z",
        "source": "web", "retries": 0, "approved": False, "approval_status": "pending",
        "approved_channels": [], "published_to": [], "task_type": "image", "min_vram_mb": 4096, "max_retries": 3,
        "brand_id": "brand01", "aspect_ratio": "16:9", "output_preset": "youtube",
        "version": 1, "stages": {},
        "scenes": [
            {"prompt": "Перша сцена", "voiceover": "Озвучка 1", "duration": 5},
            {"prompt": "Друга сцена", "voiceover": "Озвучка 2", "duration": 5},
        ],
        "artifacts": [], "events": ["SCRIPT_PENDING_APPROVAL"],
        "publication_results": {}, "active_task_ids": {}, "completed_task_ids": [],
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
        page_errors, console_errors = _attach_error_collector(page)
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
        page.route("**/api/jobs/job-script-01/**", handle_detail)
        page.route("**/api/characters", lambda route: route.fulfill(json=[]))
        page.route("**/api/brands", lambda route: route.fulfill(json=[]))
        page.route("**/api/brands/**", lambda route: route.fulfill(json=[]))
        page.route("**/api/workflows", lambda route: route.fulfill(json=[]))
        page.route("**/api/channels/types", lambda route: route.fulfill(json=[]))
        page.route("**/api/channels/**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs/job-script-01")
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(1000)

        expect(page.locator("[data-testid='job-detail-page']")).to_be_visible()
        expect(page.locator("[data-testid='job-script']")).to_be_visible()
        expect(page.locator("[data-testid='approve-script-button']")).to_be_visible()
        expect(page.locator("[data-testid='job-script']")).to_contain_text("Новий ролик")

        page.locator("[data-testid='approve-script-button']").click()
        expect(page.locator("[data-testid='approve-script-button']")).not_to_be_visible()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Сценарій затверджено")
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_script_revision_happy_path():
    """i.0.0.0.4: script revision prompt posts revision to backend."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        def handle_job_detail(route):
            if route.request.method == "POST" and "/script/revision" in route.request.url:
                route.fallback()
                return
            route.fulfill(json=job)
        page.route("**/api/jobs/job-script-01/script/revision", handle_revision)
        page.route("**/api/jobs/job-script-01", handle_job_detail)
        page.route("**/api/jobs/job-script-01/**", handle_job_detail)
        page.route("**/api/characters", lambda route: route.fulfill(json=[]))
        page.route("**/api/brands", lambda route: route.fulfill(json=[]))
        page.route("**/api/brands/**", lambda route: route.fulfill(json=[]))
        page.route("**/api/workflows", lambda route: route.fulfill(json=[]))
        page.route("**/api/channels/types", lambda route: route.fulfill(json=[]))
        page.route("**/api/channels/**", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/jobs/job-script-01")
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(1000)

        with page.expect_response("**/api/jobs/job-script-01/script/revision"):
            page.locator("[data-testid='revision-script-button']").click()
        assert revised is not None, "script/revision call was not made"
        assert revised.get("revision") == "зробити сцени коротшими", revised
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Запитані правки")
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()
def test_script_backend_error_shows_error_and_no_crash():
    """i.0.0.0.4: backend error on approve surfaces an action error without crashing the page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)

        job = _script_job()

        def handle_approve(route):
            route.fulfill(status=500, content_type="application/json",
                          body='{"detail": "Сценарій недоступний для затвердження"}')

        page.route("**/api/jobs/job-script-01/script/approve", handle_approve)
        page.route("**/api/jobs/job-script-01", lambda route: route.fulfill(json=job))
        page.goto(f"{BASE_URL}/jobs/job-script-01")
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(1000)

        page.locator("[data-testid='approve-script-button']").click()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Сценарій недоступний для затвердження")
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
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
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        # Issue #36: the actual scene images are rendered inline (not only an id/link).
        scene_image = page.locator("[data-testid='scene-preview-image']")
        expect(scene_image).to_have_count(2)
        expect(scene_image.first).to_have_attribute(
            "src", "/api/jobs/job-sb-01/artifacts/art-s1/download")

        # Approve the storyboard via the general approve button (canReviewStoryboard).
        page.get_by_role("button", name="Схвалити", exact=True).click()
        expect(page.locator("[data-testid='job-detail-page']")).to_contain_text("Розкадровка затверджена")
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_storyboard_stale_version_conflict():
    """i.0.0.0.4: stale-version conflict (409) on image preview approval surfaces an error."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
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
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
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
        page_errors, console_errors = _attach_error_collector(page)
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
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_setup_wizard_core_hides_connection_fields():
    """Core role selection hides core connection form."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_setup_wizard_configured_redirects_home():
    """If already configured, setup page redirects to /."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_setup(page, configured=True)
        page.goto(f"{BASE_URL}/setup?token=ci")
        page.wait_for_timeout(500)
        assert page.url.rstrip("/") == BASE_URL or page.url == f"{BASE_URL}/"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_setup_wizard_shows_hardware():
    """Hardware section shows detected hardware info."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        hw = {"cpu": "AMD Ryzen 9", "ram_mb": 32768,
              "gpu": {"vendor": "nvidia", "name": "RTX 4090"},
              "docker_version": "24.0.7"}
        _mock_setup(page, hardware=hw)
        page.goto(f"{BASE_URL}/setup?token=ci")
        for _ in range(4):
            page.locator("#next").click()
            page.wait_for_timeout(100)
        expect(page.locator("#hardware")).to_contain_text("RTX 4090")
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_setup_wizard_health_check_displays_results():
    """Health check step fetches and renders check results."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_setup_wizard_back_button_hidden_on_first_step():
    """Back button is invisible on step 0."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_setup(page)
        page.goto(f"{BASE_URL}/setup?token=ci")
        expect(page.locator("#back")).to_have_css("visibility", "hidden")
        page.locator("#next").click()
        page.wait_for_timeout(100)
        expect(page.locator("#back")).to_have_css("visibility", "visible")
        _assert_no_js_errors(page_errors, console_errors)
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
        page_errors, console_errors = _attach_error_collector(page)
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
        assert page_errors == [], f"pageerror: {page_errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()
def test_publication_flow_launches_and_shows_result():
    """i.0.0.0.17: publication flow posts /jobs/{id}/publish and renders results."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        assert page_errors == [], f"pageerror: {page_errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_fleet_enrollment_issues_token_and_exposes_register_endpoint():
    """i.0.0.0.17: fleet enrollment issues a registration token and shows the register URL."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        page.route("**/api/workers*", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/workers")
        expect(page.locator("[data-testid='workers-page']")).to_be_visible()

        page.locator("[data-testid='create-worker-button']").click()
        page.locator("[data-testid='worker-role-select']").select_option("gpu")
        page.locator("[data-testid='generate-token-button']").click()
        expect(page.locator("[data-testid='token-display']")).to_contain_text("VT-ENR-LL-ROLE01")
        expect(page.locator("[data-testid='token-display']")).to_contain_text("/api/nodes/register")
        assert requested_roles, "registration-tokens POST was not made"
        assert page_errors == [], f"pageerror: {page_errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()
def test_update_critical_mutations_install_and_canary():
    """i.0.0.0.17: update critical mutations (install / promote / rollback) hit the backend."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)

        update_status = {
            "current_version": "0.0.1.53", "available_version": "0.0.2.0",
            "state": "IDLE", "phase": "NORMAL", "progress": 0,
            "message": "Очікування", "log": [], "enabled": True, "pending": 0,
            "update_available": True,
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
        page.locator("a[href='/settings?tab=update']").click()
        expect(page.locator("[data-testid='settings-update']")).to_be_visible()

        with page.expect_response("**/api/system/update/run"):
            page.locator("[data-testid='update-install']").click()
        with page.expect_response("**/api/system/update/rolling/promote"):
            page.locator("[data-testid='update-promote-canary']").click()
        with page.expect_response("**/api/system/update/rolling/rollback"):
            page.locator("[data-testid='update-rollback-canary']").click()
        with page.expect_response("**/api/system/recovery/normal"):
            page.locator("[data-testid='update-recover-normal']").click()

        assert "install" in post_calls, post_calls
        assert "promote" in post_calls, post_calls
        assert "rollback" in post_calls, post_calls
        assert "recover" in post_calls, post_calls
        assert page_errors == [], f"pageerror: {page_errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_update_center_renders_progress_recovery_and_localized_controls():
    """#106: Update Center exposes backend progress/recovery without broken UI text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/system/update", lambda route: route.fulfill(json={
            "current_version": "0.0.2.0", "available_version": "0.0.2.1",
            "state": "FAILED", "phase": "RECOVERING", "progress": 42,
            "message": "Перевірка стану після помилки", "log": [],
            "enabled": True, "pending": 0, "update_available": True,
        }))
        page.route("**/api/system/update/readiness", lambda route: route.fulfill(json={
            "ready": False, "inflight": 2, "busy_workers": ["gpu-1"],
            "active_jobs": [], "queue_paused": True, "drain_operation_id": "op-1",
            "acknowledged_workers": [], "unacknowledged_workers": ["gpu-1"],
        }))
        page.route("**/api/system/update/rolling", lambda route: route.fulfill(json={
            "state": "PAUSED", "current_batch": 1, "total_batches": 3,
        }))

        page.goto(f"{BASE_URL}/settings?tab=update")
        expect(page.locator("[data-testid='settings-update']")).to_be_visible()
        expect(page.locator("[data-testid='update-progress']")).to_contain_text("42%")
        expect(page.locator("[data-testid='update-progress']")).to_contain_text(
            "Перевірка стану після помилки")
        expect(page.locator("[data-testid='update-recovery-state']")).to_be_visible()
        text = page.locator("[data-testid='settings-update']").inner_text()
        assert "�" not in text
        assert "Busy" not in text and "Promote canary" not in text and "Rollback canary" not in text
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"horizontal overflow in Update Center: {overflow}px"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_viewer_cannot_see_or_open_admin_settings():
    """#106: role/permission UI hides Settings and its route from viewers."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page, role="viewer")
        _mock_status(page)
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))
        page.route("**/api/workers*", lambda route: route.fulfill(json=[]))

        page.goto(f"{BASE_URL}/")
        expect(page.locator("[data-testid='dashboard']")).to_be_visible()
        expect(page.get_by_role("link", name="Система", exact=True)).to_have_count(0)
        page.goto(f"{BASE_URL}/settings")
        expect(page.locator("[data-testid='settings-page']")).to_have_count(0)
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()
def test_backup_critical_mutations_create_and_restore():
    """i.0.0.0.17: backup critical mutations (create / restore) hit the backend."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        page.locator("a[href='/settings?tab=backup']").click()
        expect(page.locator("[data-testid='settings-backup']")).to_be_visible()

        page.locator("[data-testid='backup-create-button']").click()
        expect(page.locator("[data-testid='backup-restore-button']").first).to_be_visible()
        page.locator("[data-testid='backup-restore-button']").first.click()
        page.get_by_role("button", name="Підтвердити").click()

        assert "create" in post_calls, post_calls
        assert "restore" in post_calls, post_calls
        assert page_errors == [], f"pageerror: {page_errors}"
        assert console_errors == [], f"console.error: {console_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def _mock_chrome_data(page):
    """i.0.0.0.28: generic list endpoints so header/sidebar chrome tests avoid page errors."""
    _mock_session(page)
    _mock_status(page)
    page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))
    page.route("**/api/workers*", lambda route: route.fulfill(json=[]))
    page.route("**/api/characters", lambda route: route.fulfill(json=[]))
    page.route("**/api/workflows", lambda route: route.fulfill(json=[]))
    page.route("**/api/brands", lambda route: route.fulfill(json=[]))


def test_header_metadata_matches_navigation():
    """i.0.0.0.28: header title/subtitle follow the URL and sidebar active state is correct."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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

        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_localization_scan_no_raw_english_chrome():
    """i.0.0.0.28: sidebar/header chrome carries no stray English UI labels."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_collapsed_sidebar_icons_tooltips_no_overflow():
    """i.0.0.0.28: collapsed sidebar exposes icons with tooltips and no page overflow."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
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
        assert page_errors == [], f"pageerror: {page_errors}"
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


def test_loading_state_resolves_to_content_or_error():
    """i.0.0.0.28: loading state does not hang forever — it resolves to data or error."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        _mock_session(page)
        _mock_status(page)
        page.route("**/api/jobs*", lambda route: route.fulfill(json=[]))
        page.route("**/api/workers*", lambda route: route.fulfill(json=[]))
        page.goto(f"{BASE_URL}/")

        # Dashboard must reach a terminal state — loading must not hang forever.
        page.wait_for_timeout(2000)
        expect(page.locator("[data-testid='loading-state']")).not_to_be_visible()
        terminal_states = page.locator(
            "[data-testid='empty-state'], [data-testid='error-state'], [data-testid='workers-table-section'], [data-testid='stat-workers']"
        )
        expect(terminal_states.first).to_be_visible()
        _assert_no_js_errors(page_errors, console_errors)
        browser.close()


# ── Issue #75 S6: session/account acceptance against a real local backend ──
# Ці тести не мокують /api/session, /api/session/profile, /api/session/password
# чи /api/status: вони доводять реальну серверну identity, RBAC і system-state
# поведінку на запущеному CORE, а не frontend-контракт.
# Облікові записи E2E задаються через USERS_JSON (окремі від ADMIN_USER енва,
# щоб ротація пароля не залишала другий дійсний шлях входу).
ADMIN_USER = os.getenv("VERTEP_E2E_ADMIN_USER", "e2e-admin")
ADMIN_PASSWORD = os.getenv("VERTEP_E2E_ADMIN_PASSWORD", "e2e-admin-password-12")
VIEWER_USER = os.getenv("VERTEP_E2E_VIEWER_USER", "e2e-viewer")
VIEWER_PASSWORD = os.getenv("VERTEP_E2E_VIEWER_PASSWORD", "e2e-viewer-password-12")
ROTATED_ADMIN_PASSWORD = os.getenv("VERTEP_E2E_ROTATED_PASSWORD", "e2e-admin-rotated-12")
# tests/conftest.py перенаправляє UPDATE_STATE_DIR у герметичний тимчасовий каталог,
# тому стан керованої системи задається окремим шляхом до каталогу, який читає
# запущений CORE. Без нього system-state перевірка не виконується (skip, не pass).
E2E_STATE_DIR = os.getenv("VERTEP_E2E_STATE_DIR")


def _state_path():
    return Path(E2E_STATE_DIR) / "system-state.json"


def _write_system_state(state: str, reason: str = ""):
    """Drive the durable system state store the running CORE reads."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"state": state, "updated_at": "2026-01-01T00:00:00+00:00",
                    "reason": reason, "operation_id": None}),
        encoding="utf-8",
    )


def _csrf_headers(context) -> dict:
    """CSRF header the UI sends for its own mutations."""
    for cookie in context.cookies():
        if cookie["name"] == "vertep_csrf":
            return {"X-CSRF-Token": cookie["value"]}
    raise AssertionError("vertep_csrf cookie is missing for the signed-in session")


def _ui_login(page, user: str, password: str):
    page.goto(f"{BASE_URL}/login")
    expect(page.get_by_role("heading", name="Вхід")).to_be_visible(timeout=20000)
    page.locator("input[type='text']").fill(user)
    page.locator("input[type='password']").fill(password)
    page.get_by_role("button", name="Увійти").click()
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    except PlaywrightTimeout as error:
        # Непрозорий таймаут діагностується через причину відмови сервера,
        # інакше незрозуміло, чи це credentials, CSRF чи недоступний CORE.
        session = page.request.get(f"{BASE_URL}/api/session")
        error_text = page.get_by_test_id("login-error")
        detail = error_text.inner_text() if error_text.count() else "(без тексту помилки)"
        raise AssertionError(
            f"UI login for {user!r} did not leave /login: "
            f"/api/session -> {session.status}, login error: {detail}"
        ) from error


def _ui_logout(page):
    page.locator("button[aria-label='Меню користувача']").click()
    page.get_by_role("button", name="Вийти").click()
    page.wait_for_url(f"{BASE_URL}/login", timeout=20000)


def test_real_backend_admin_profile_password_logout_roundtrip():
    """Issue #75 S1/S3/S6: identity, поля облікового запису, пароль і logout."""
    display_name = "Browser E2E Admin"
    email = "browser-e2e-admin@example.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, ADMIN_USER, ADMIN_PASSWORD)
            expect(page.get_by_text("Адмін", exact=True)).to_be_visible(timeout=20000)
            expect(page.get_by_text("Адміністрування", exact=True)).to_be_visible()

            # Профіль показує серверну identity, а не локальні припущення.
            page.goto(f"{BASE_URL}/profile")
            expect(page.get_by_test_id("profile-page")).to_be_visible(timeout=20000)
            expect(page.get_by_test_id("profile-login")).to_have_text(ADMIN_USER)
            expect(page.get_by_test_id("profile-role")).to_have_text("Адміністратор")
            expect(page.get_by_test_id("password-policy-hint")).to_contain_text("12")

            # Поля імені/email зберігаються на сервері й лишаються після reload.
            page.get_by_test_id("display-name-input").fill(display_name)
            page.get_by_test_id("email-input").fill(email)
            page.get_by_test_id("account-save").click()
            page.wait_for_timeout(500)
            page.reload()
            expect(page.get_by_test_id("display-name-input")).to_have_value(display_name)
            expect(page.get_by_test_id("email-input")).to_have_value(email)
            session_body = page.request.get(f"{BASE_URL}/api/session").json()
            assert session_body["role"] == "admin"
            assert session_body["display_name"] == display_name
            assert session_body["email"] == email

            # Невірний поточний пароль — відновлювана помилка, сесія лишається чинною.
            page.get_by_test_id("old-password-input").fill("definitely-not-the-password")
            page.get_by_test_id("new-password-input").fill(ROTATED_ADMIN_PASSWORD)
            page.get_by_test_id("confirm-password-input").fill(ROTATED_ADMIN_PASSWORD)
            page.get_by_test_id("password-save").click()
            expect(page.get_by_test_id("password-error")).to_be_visible(timeout=20000)
            assert page.request.get(f"{BASE_URL}/api/session").json()["authenticated"] is True

            # Успішна зміна: новий пароль приймає сервер, старий — ні.
            page.get_by_test_id("old-password-input").fill(ADMIN_PASSWORD)
            page.get_by_test_id("new-password-input").fill(ROTATED_ADMIN_PASSWORD)
            page.get_by_test_id("confirm-password-input").fill(ROTATED_ADMIN_PASSWORD)
            page.get_by_test_id("password-save").click()
            page.wait_for_timeout(800)

            _ui_logout(page)
            expect(page.get_by_role("heading", name="Вхід")).to_be_visible()
            page.locator("input[type='text']").fill(ADMIN_USER)
            page.locator("input[type='password']").fill(ADMIN_PASSWORD)
            page.get_by_role("button", name="Увійти").click()
            expect(page.get_by_test_id("login-error")).to_be_visible(timeout=20000)

            _ui_login(page, ADMIN_USER, ROTATED_ADMIN_PASSWORD)
            expect(page.get_by_text("Адмін", exact=True)).to_be_visible(timeout=20000)
        finally:
            # Середовище повертається до початкового пароля, тест не залишає
            # змінених облікових даних для наступних прогонів.
            _restore_admin_password(browser)
            browser.close()


def _restore_admin_password(browser):
    """Rotate the administrator password back to the configured value."""
    import base64
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{BASE_URL}/login")
        page.locator("input[type='text']").fill(ADMIN_USER)
        page.locator("input[type='password']").fill(ROTATED_ADMIN_PASSWORD)
        page.get_by_role("button", name="Увійти").click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
        page.goto(f"{BASE_URL}/profile")
        expect(page.get_by_test_id("password-form")).to_be_visible(timeout=20000)
        page.get_by_test_id("old-password-input").fill(ROTATED_ADMIN_PASSWORD)
        page.get_by_test_id("new-password-input").fill(ADMIN_PASSWORD)
        page.get_by_test_id("confirm-password-input").fill(ADMIN_PASSWORD)
        page.get_by_test_id("password-save").click()
        page.wait_for_timeout(500)
    finally:
        context.close()


def test_real_backend_viewer_has_no_admin_access():
    """Issue #75 S1/S6: справжній viewer не отримує admin UI та admin API."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, VIEWER_USER, VIEWER_PASSWORD)
            expect(page.get_by_text("Переглядач", exact=False).first).to_be_visible(timeout=20000)
            expect(page.locator("nav").first.get_by_text("Адміністрування", exact=True)).to_have_count(0)

            body = page.request.get(f"{BASE_URL}/api/session").json()
            assert body["authenticated"] is True
            assert body["user"] == VIEWER_USER
            assert body["role"] == "viewer"

            # Прямі API-негативні перевірки: UI не є доказом серверного RBAC.
            create = page.request.post(f"{BASE_URL}/api/jobs", data={"topic": "viewer probe"})
            assert create.status == 403, f"viewer job create returned {create.status}"
            settings = page.request.post(f"{BASE_URL}/api/settings/logo")
            assert settings.status == 403, f"viewer settings mutation returned {settings.status}"

            # Власний профіль і власний пароль viewer недоступні адміністратору,
            # але доступні самому власнику акаунта.
            page.goto(f"{BASE_URL}/profile")
            expect(page.get_by_test_id("profile-page")).to_be_visible(timeout=20000)
            expect(page.get_by_test_id("profile-role")).to_have_text("Переглядач")
            page.get_by_test_id("display-name-input").fill("Browser E2E Viewer")
            page.get_by_test_id("email-input").fill("browser-e2e-viewer@example.com")
            page.get_by_test_id("account-save").click()
            page.wait_for_timeout(500)
            page.reload()
            expect(page.get_by_test_id("display-name-input")).to_have_value("Browser E2E Viewer")

            _ui_logout(page)
            expect(page.get_by_role("heading", name="Вхід")).to_be_visible()
            _assert_no_js_errors(page_errors, console_errors)
        finally:
            browser.close()


def test_real_backend_system_state_blocks_mutations():
    """Issue #75 S6: реальний system-state блокує мутації в UI та API."""
    if not E2E_STATE_DIR:
        pytest.skip("VERTEP_E2E_STATE_DIR is not set: the running CORE state store "
                    "cannot be addressed from the isolated test process")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, ADMIN_USER, ADMIN_PASSWORD)
            headers = _csrf_headers(page.context)
            # Порожня тема не створює завдання: у NORMAL запит доходить до
            # валідації (422), у READ_ONLY його блокує system-state (423).
            _write_system_state("READ_ONLY", "Browser E2E read-only")
            try:
                page.goto(f"{BASE_URL}/jobs")
                expect(page.get_by_test_id("jobs-page")).to_be_visible(timeout=20000)
                blocked = page.request.post(f"{BASE_URL}/api/jobs", data={"topic": ""}, headers=headers)
                assert blocked.status == 423, f"READ_ONLY job create returned {blocked.status}"
                assert "blocked by system state" in blocked.text()
                # UI читає системний стан під час ініціалізації застосунку,
                # тому перезавантаження робить перевірку детермінованою.
                page.reload()
                create_button = page.get_by_test_id("create-job-button")
                expect(create_button).to_be_disabled(timeout=20000)
                assert "READ_ONLY" in (create_button.get_attribute("title") or "")
            finally:
                _write_system_state("NORMAL", "Browser E2E restored")
            page.wait_for_timeout(500)
            page.reload()
            expect(page.get_by_test_id("jobs-page")).to_be_visible(timeout=20000)
            expect(page.get_by_test_id("create-job-button")).to_be_enabled(timeout=20000)
            allowed = page.request.post(f"{BASE_URL}/api/jobs", data={"topic": ""}, headers=headers)
            assert allowed.status != 423, f"NORMAL job create was blocked: {allowed.status}"
            _assert_no_js_errors(page_errors, console_errors)
        finally:
            _write_system_state("NORMAL", "Browser E2E teardown")
            browser.close()


# ── Issue #122 P8: video engine against a real backend ─────────────────────
# Ці тести не мокують /api/settings/video-engine чи /api/settings/providers/*:
# вони доводять, що екран показує саме той движок, який запущений CORE обрав для
# себе, і що відмова застосування не лишає на екрані нічого, чого не відбулося.

def _engine_state(page) -> dict:
    response = page.request.get(f"{BASE_URL}/api/settings/video-engine")
    assert response.status == 200, response.text()
    return response.json()


def test_real_backend_video_engine_shows_the_effective_executor():
    """Відображений вибір дорівнює руху, який реально обрав процес."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, ADMIN_USER, ADMIN_PASSWORD)
            page.goto(f"{BASE_URL}/settings?tab=video-engine")
            expect(page.get_by_test_id("settings-video-engine")).to_be_visible(timeout=20000)
            expect(page.get_by_test_id("video-engine-panel")).to_be_visible(timeout=20000)

            config = _engine_state(page)
            labels = {option["id"]: option["label"] for option in config["options"]}
            assert labels["native"] == "Vertep Native"
            assert labels["money-printer"] == "MoneyPrinterTurbo"
            expect(page.get_by_test_id("video-engine-selected")).to_have_text(
                labels[config["selected"]], timeout=20000)
            expect(page.get_by_test_id("video-engine-effective")).to_have_text(
                labels[config["effective"]], timeout=20000)
            expect(page.get_by_test_id("video-engine-revision")).to_have_text(
                config["config_revision"])

            # Той самий engine має бути й у матриці активних бекендів: це те, що
            # обрав процес, а не лише те, що надіслав браузер.
            matrix = page.request.get(f"{BASE_URL}/api/settings/providers").json()["matrix"]
            assert matrix["video_engine"]["selected"] == config["effective"]
            if config["effective"] != "native":
                assert matrix["video_engine"]["configured"] is True, (
                    "an effective external engine that is not configured could not be "
                    "what the screen shows"
                )

            # Перезавантаження не змінює показане: воно знову читає те саме API.
            page.reload()
            expect(page.get_by_test_id("video-engine-effective")).to_have_text(
                labels[_engine_state(page)["effective"]], timeout=20000)
            _assert_no_js_errors(page_errors, console_errors)
        finally:
            browser.close()


def test_real_backend_video_engine_refuses_a_runtime_it_cannot_prove():
    """Відмова застосування видима, а рух лишається тим, який був."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, ADMIN_USER, ADMIN_PASSWORD)
            headers = _csrf_headers(page.context)
            before = _engine_state(page)
            # Адреса, яку ніхто не слухає: застосування мусить відмовититися.
            page.goto(f"{BASE_URL}/settings?tab=video-engine")
            expect(page.get_by_test_id("video-engine-panel")).to_be_visible(timeout=20000)
            page.select_option("[data-testid='video-engine-select']", "money-printer")
            page.fill("[data-testid='video-engine-endpoint']", "http://127.0.0.1:9/")
            page.click("[data-testid='video-engine-apply']")

            expect(page.get_by_test_id("video-engine-error")).to_be_visible(timeout=30000)
            expect(page.get_by_test_id("video-engine-rollback")).to_contain_text(
                "Попередній движок збережено")

            after = _engine_state(page)
            assert after["effective"] == before["effective"], \
                "a refused apply must not change the effective engine"
            assert after["config_revision"] == before["config_revision"]
            labels = {option["id"]: option["label"] for option in after["options"]}
            expect(page.get_by_test_id("video-engine-effective")).to_have_text(
                labels[after["effective"]], timeout=20000)

            # Той самий запит напряму: refusal, не мовчазливий Native.
            refused = page.request.post(
                f"{BASE_URL}/api/settings/providers/video_engine",
                data={"backend": "money-printer", "endpoint": "http://127.0.0.1:9"},
                headers=headers,
            )
            assert refused.status == 409, f"unprovable runtime returned {refused.status}"
            assert _engine_state(page)["effective"] == before["effective"]
            _assert_no_js_errors(page_errors, console_errors)
        finally:
            browser.close()


def test_real_backend_viewer_cannot_change_the_video_engine():
    """P8 RBAC: the engine controls are admin-only in the UI and in the API.

    ``/settings`` is behind the existing admin guard, so a viewer is redirected away
    instead of being offered the control; the API refuses the same switch even if the
    request is made directly.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, VIEWER_USER, VIEWER_PASSWORD)
            page.goto(f"{BASE_URL}/settings?tab=video-engine")
            expect(page.get_by_test_id("settings-video-engine")).not_to_be_visible(timeout=20000)
            assert "/settings" not in page.url, \
                f"a viewer reached the admin settings screen: {page.url}"

            before = _engine_state(page)
            refused = page.request.post(
                f"{BASE_URL}/api/settings/providers/video_engine",
                data={"backend": "money-printer", "endpoint": "http://127.0.0.1:9"},
                headers=_csrf_headers(page.context),
            )
            assert refused.status == 403, f"viewer engine switch returned {refused.status}"
            after = _engine_state(page)
            assert after["effective"] == before["effective"]
            assert after["config_revision"] == before["config_revision"]
            _assert_no_js_errors(page_errors, console_errors)
        finally:
            browser.close()


def test_real_backend_system_state_locks_the_video_engine():
    """Системний стан, що забороняє конфігурацію, блокує і цей екран."""
    if not E2E_STATE_DIR:
        pytest.skip("VERTEP_E2E_STATE_DIR is not set: the running CORE state store "
                    "cannot be addressed from the isolated test process")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors, console_errors = _attach_error_collector(page)
        try:
            _ui_login(page, ADMIN_USER, ADMIN_PASSWORD)
            headers = _csrf_headers(page.context)
            _write_system_state("READ_ONLY", "Browser E2E video engine lock")
            try:
                blocked = page.request.post(
                    f"{BASE_URL}/api/settings/providers/video_engine",
                    data={"backend": "money-printer", "endpoint": "http://127.0.0.1:9"},
                    headers=headers,
                )
                assert blocked.status == 423, blocked.text()
                page.goto(f"{BASE_URL}/settings?tab=video-engine")
                expect(page.get_by_test_id("video-engine-locked")).to_be_visible(timeout=20000)
                expect(page.get_by_test_id("video-engine-apply")).to_be_disabled()
                assert _engine_state(page)["change_allowed"] is False
            finally:
                _write_system_state("NORMAL", "Browser E2E restored")
            # UI reads the system state while the app initialises, so the restore is
            # made deterministic by reloading after a short pause.
            page.wait_for_timeout(500)
            page.reload()
            expect(page.get_by_test_id("video-engine-locked")).not_to_be_visible(timeout=20000)
            restored = _engine_state(page)
            assert restored["change_allowed"] is True
            assert restored["system_state"] == "NORMAL"
            allowed = page.request.post(
                f"{BASE_URL}/api/settings/providers/video_engine",
                data={"backend": "native"},
                headers=headers,
            )
            assert allowed.status != 423, f"NORMAL engine switch was blocked: {allowed.status}"
            _assert_no_js_errors(page_errors, console_errors)
        finally:
            _write_system_state("NORMAL", "Browser E2E teardown")
            browser.close()
