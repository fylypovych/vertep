"""#352 remaining state matrix against a disposable CORE, without API payload mocks."""
import json
import re
import importlib.util
import fnmatch

import pytest

from tests.acceptance_provenance import acceptance_provenance
from tests.test_monitoring_browser_e2e import monitoring_core, monitoring_page
from tests.test_media_browser_e2e import expect
from tests.test_monitoring_browser_e2e import ROOT


CASES = [
    ("jobs", "/jobs", "jobs-page", "**/api/jobs?*", "app-loading-state", "app-error-state", "Повторити"),
    ("queue", "/queue", "queue-page", "**/api/tasks/queue", ".animate-pulse", "[data-testid='queue-error']", "Оновити"),
    ("fleet", "/workers", "workers-page", "**/api/workers", "app-loading-state", "app-error-state", "Повторити"),
    ("dashboard", "/", "dashboard", "**/api/workers", "app-loading-state", "app-error-state", "Повторити"),
    ("settings", "/settings", "settings-system-info", "**/api/status", "app-loading-state", "app-error-state", "Повторити"),
    ("alerts", "/alerts", "alerts-page", "**/api/alerts*", "app-loading-state", "app-error-state", "Повторити"),
    ("logs", "/logs", "logs-page", "**/api/logs?*", "app-loading-state", "app-error-state", "Повторити"),
    ("health", "/health", "health-page", "**/api/health", "app-loading-state", "app-error-state", "Повторити"),
]


@pytest.mark.parametrize("name,path,testid,endpoint,loading,error,retry", CASES, ids=[c[0] for c in CASES])
def test_read_loading_failure_retry_without_mutation(
        monitoring_core, monitoring_page, name, path, testid, endpoint, loading, error, retry):
    page, core = monitoring_page, monitoring_core
    pending, writes = [], []
    fault = {"active": False}
    page.on("request", lambda request: writes.append(request.url)
            if "/api/" in request.url and request.method not in {"GET", "HEAD", "OPTIONS"} else None)
    page.route(endpoint, lambda route: route.abort("failed") if fault["active"] else pending.append(route))
    page.goto(core["url"] + path)
    view = page.get_by_test_id(testid)
    expect(view.locator(loading).first).to_be_visible(timeout=20000)
    # Actual network failure, no fabricated response or orchestration result.
    fault["active"] = True
    for route in list(pending):
        route.abort("failed")
    expect(view.locator(error).first).to_be_visible(timeout=20000)
    page.unroute(endpoint)
    with page.expect_response(lambda response: fnmatch.fnmatch(response.url, endpoint)
                              and response.request.method == "GET"):
        view.get_by_role("button", name=retry, exact=True).first.click()
    expect(view.locator(loading).first).not_to_be_visible(timeout=30000)
    expect(view.locator(error).first).not_to_be_visible(timeout=30000)
    if name == "jobs":
        expect(view.get_by_test_id("jobs-empty")).to_be_visible()
    elif name == "fleet":
        expect(view.get_by_test_id("workers-empty")).to_be_visible()
    elif name == "alerts":
        expect(view.locator("app-empty-state")).to_be_visible()
    assert writes == []
    (core["root"] / "state-evidence.json").write_text(json.dumps({
        "surface": name, "loading": True, "network_error": True,
        "actual_retry": True, "api_writes": writes,
    }), encoding="utf-8")


def test_channels_read_retry_mutation_and_permission(monitoring_core, monitoring_page):
    core, page = monitoring_core, monitoring_page
    api = core["api"]
    response = api.post("/api/brands", json={"id": "statebrand", "name": "State matrix brand"})
    assert response.status_code == 200, response.text
    endpoint = "**/api/brands/statebrand/channels"
    pending = []
    page.route(endpoint, lambda route: pending.append(route))
    page.goto(core["url"] + "/brands")
    channels = page.get_by_test_id("brand-channels-statebrand")
    expect(channels.locator("app-loading-state")).to_be_visible()
    for route in list(pending):
        route.abort("failed")
    page.unroute(endpoint)
    expect(channels.locator("app-error-state")).to_be_visible()
    channels.get_by_role("button", name="Повторити", exact=True).click()
    expect(channels.get_by_text("Каналів не налаштовано", exact=True)).to_be_visible()
    assert api.get("/api/brands/statebrand/channels").json() == []
    channels.get_by_role("button", name="Додати канал", exact=True).click()
    channels.locator("select").select_option(index=0)
    channels.locator("input").fill("synthetic-state-channel")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/channels")) as created:
        channels.get_by_role("button", name="Створити", exact=True).click()
    assert created.value.ok, created.value.text()
    saved = api.get("/api/brands/statebrand/channels").json()
    assert len(saved) == 1 and saved[0]["target"] == "synthetic-state-channel"
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/channels/" in r.url) as toggled:
        channels.get_by_test_id("channel-toggle").click()
    assert toggled.value.ok
    before = api.get("/api/brands/statebrand/channels").json()
    assert before[0]["enabled"] is False
    response = api.post("/api/system/state", json={"reason": "disposable permission acceptance"})
    assert response.status_code == 200, response.text
    page.reload()
    expect(channels.get_by_test_id("channel-toggle")).to_be_disabled()
    denied = api.put("/api/channels/" + before[0]["channel_id"], json={"enabled": True})
    assert denied.status_code == 423, denied.text
    assert api.get("/api/brands/statebrand/channels").json() == before


def test_keyboard_modal_focus_cancel_has_no_mutation(monitoring_core, monitoring_page):
    core, page = monitoring_core, monitoring_page
    api = core["api"]
    assert api.post("/api/brands", json={"id": "focusbrand", "name": "Focus brand"}).status_code == 200
    kind = api.get("/api/channels/types").json()[0]
    response = api.post("/api/brands/focusbrand/channels", json={
        "brand_id": "focusbrand", "channel_type": kind, "target": "focus-target"})
    assert response.status_code == 200, response.text
    before = api.get("/api/brands/focusbrand/channels").json()
    page.goto(core["url"] + "/brands")
    trigger = page.get_by_test_id("brand-channels-focusbrand").get_by_test_id("channel-delete")
    expect(trigger).to_be_enabled()
    trigger.focus()
    expect(trigger).to_be_focused()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    confirm = dialog.get_by_role("button", name="Підтвердити", exact=True)
    cancel = dialog.get_by_role("button", name="Скасувати", exact=True)
    expect(confirm).to_be_focused()
    contrast = confirm.evaluate("""el => {
      const style = getComputedStyle(el), canvas = document.createElement('canvas');
      canvas.width = canvas.height = 1;
      const ctx = canvas.getContext('2d');
      const luminance = color => {
        ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 1, 1);
        ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1);
        const rgb = [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3).map(v => {
          v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
        });
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      const a = luminance(style.color), b = luminance(style.backgroundColor);
      return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    }""")
    assert contrast >= 4.5
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(confirm).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()
    assert api.get("/api/brands/focusbrand/channels").json() == before


def test_routes_localized_dates_and_responsive_layout(monitoring_core, monitoring_page):
    core, page = monitoring_core, monitoring_page
    api = core["api"]
    assert api.post("/api/logs/ingest", json={"node_name": "date-proof", "entries": [
        {"message": "localized-date-proof", "level": "INFO"}]}).status_code == 200
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 844})
        for path, title in (("/jobs", "Завдання"), ("/queue", "Черга"),
                            ("/workers", "Вузли"), ("/logs", "Журнали"),
                            ("/alerts", "Алерти"), ("/health", "Стан системи"),
                            ("/settings?tab=system", "Налаштування"), ("/brands", "Бренди")):
            page.goto(core["url"] + path)
            expect(page.get_by_test_id("header-title")).to_have_text(title)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.goto(core["url"] + "/logs")
    row = page.get_by_test_id("logs-table").locator("tr").filter(has_text="localized-date-proof")
    expect(row).to_be_visible()
    expect(row.locator("td").first).to_have_text(re.compile(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}"))


def test_existing_route_localization_and_runner_smoke(monitoring_core, monkeypatch):
    """Reuse the established UI-only smoke, independently of actual Runner proof."""
    monkeypatch.setenv("VERTEP_URL", monitoring_core["url"])
    spec = importlib.util.spec_from_file_location("isolated_web_smoke", ROOT / "tests/test_browser_e2e.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    for name in ("test_header_metadata_matches_navigation", "test_localization_scan_no_raw_english_chrome",
                 "test_collapsed_sidebar_icons_tooltips_no_overflow", "test_real_tests_run_flow_through_browser_api"):
        getattr(smoke, name)()
