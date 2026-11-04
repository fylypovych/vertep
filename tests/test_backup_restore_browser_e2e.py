"""Real-backend Browser acceptance for the Web backup restore (#401 P3).

The production web-v2 build is served by a real CORE process that calls a real Backup
Service process with healthy real dependencies; nothing on the restore route is answered
by ``page.route``. The user confirms the restore in the Browser, the two-phase flow runs
through both processes, and the restarted CORE serves the restored data from its own API.

CI sets ``VERTEP_REQUIRE_BACKUP_BROWSER_E2E=1`` (and ``VERTEP_REQUIRE_CONNECTED_RESTORE=1``)
so a missing Playwright, web build or dependency fails instead of skipping.
"""

import os
from pathlib import Path

import pytest

from tests.test_backup_process_acceptance import (  # noqa: F401
    RESTORED_WORKFLOW, backup_process, connected_core, connected_restore_dependencies,
    stop_process)

ROOT = Path(__file__).resolve().parents[1]
REQUIRE = os.getenv("VERTEP_REQUIRE_BACKUP_BROWSER_E2E") == "1"

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - dependency is required in CI
    if REQUIRE:
        pytest.fail("playwright is not installed; backup restore Browser proof is required",
                    pytrace=False)
    pytest.skip("playwright is not installed", allow_module_level=True)


def _require_web_build() -> None:
    if any((ROOT / path / "index.html").is_file() for path in (
            "web-v2/dist/vertep-admin-v2/browser", "web-v2/dist/vertep-admin-v2")):
        return
    message = "web-v2 production build is missing; run `npm run build` in web-v2"
    if REQUIRE:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def test_browser_restore_runs_through_real_core_and_backup_service(
        backup_process, connected_restore_dependencies):  # noqa: F811
    _require_web_build()
    core = connected_core(backup_process, connected_restore_dependencies, "browser")
    process, api = core["launch"]()
    url, path = core["url"], "/api/workflows/image/restore-probe.json"
    assert api.put(path, json=RESTORED_WORKFLOW).status_code == 200
    original = api.get(path).json()
    snapshot = api.post("/api/system/backups")
    assert snapshot.status_code == 200, snapshot.text
    sid = snapshot.json()["snapshot_id"]
    assert api.delete(path).status_code == 200

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        assert context.request.post(f"{url}/api/session", headers=core["auth"]).ok
        page.goto(f"{url}/settings?tab=backup")
        expect(page.get_by_test_id("settings-backup")).to_be_visible()

        # A damaged archive: both real processes refuse it, the user sees the error and
        # nothing is restored. The intact archive then restores on a retry from the UI.
        archive = backup_process["root"] / "backups" / f"{sid}.vtbackup"
        pristine = archive.read_bytes()
        archive.write_bytes(pristine + b"synthetic tampering")
        page.get_by_test_id("backup-restore-button").first.click()
        with page.expect_response(lambda response: "/restore/confirm" in response.url) as refused:
            page.get_by_role("button", name="Підтвердити").click()
        assert refused.value.status >= 400, refused.value.text()
        expect(page.get_by_test_id("backup-restore-error")).to_contain_text("409")
        expect(page.get_by_role("status")).not_to_contain_text("Відновлення завершено")
        assert api.get(path).status_code == 404, "a refused restore changed the data"
        archive.write_bytes(pristine)

        with page.expect_response(lambda response: response.url.endswith(f"/backups/{sid}/restore")
                                  and response.request.method == "POST") as challenge:
            page.get_by_test_id("backup-restore-button").first.click()
            expect(page.get_by_role("dialog", name="Відновити резервну копію")).to_be_visible()
            with page.expect_response(lambda response: "/restore/confirm" in response.url) as confirm:
                page.get_by_role("button", name="Підтвердити").click()
        assert challenge.value.ok, challenge.value.text()
        assert confirm.value.ok, confirm.value.text()
        expect(page.get_by_role("status")).to_contain_text("Відновлення завершено", timeout=60000)
        assert page_errors == [], f"pageerror: {page_errors}"
        context.close()
        browser.close()

    assert api.get(f"/api/system/backups/{sid}/restore/progress").json()["status"] == "done"
    stop_process(process)
    _, restarted = core["launch"]()
    assert restarted.get(path).json() == original
    assert restarted.get("/api/system/state").json()["state"] == "NORMAL"
