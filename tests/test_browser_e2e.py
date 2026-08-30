#!/usr/bin/env python3
"""Minimal browser E2E smoke tests for Vertep Web UI."""
import os
import sys

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    print("Playwright is not installed. Run: pip install playwright")
    sys.exit(2)


BASE_URL = os.getenv("VERTEP_URL", "http://127.0.0.1:8080")


def test_setup_page_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE_URL}/setup?token=ci")
        expect(page.locator("title")).to_contain_text("Vertep")
        browser.close()


def test_dashboard_requires_auth():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL)
        expect(page.locator("body")).to_contain_text("Увійти")
        browser.close()


def test_status_page_json():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{BASE_URL}/status")
        expect(page.locator("pre")).to_contain_text("core")
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
