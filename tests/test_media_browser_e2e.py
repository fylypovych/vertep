"""Browser -> disposable CORE -> persisted media mutations (Issue #255).

The existing Job review Browser coverage drives mocked ``page.route`` handlers,
so it cannot prove that a Web mutation reaches CORE and is persisted.  These
scenarios serve the real FastAPI app and the production Angular build on a
loopback socket and drive the real review endpoints through the Browser.

Only the external generators/providers are substituted (LLM, GPU compute, TTS
runtime, FFmpeg binary, publisher transport) through ``tests/media_harness.py``.
Jobs, the task queue, leases, review endpoints and artifact persistence stay
real.  The disposable backend is a test API, not a production installation.

Criteria: #184.2 (history selection, scene/all image revision/regenerate,
stale 409), #184.4 (Job creation, script review, video review, publication),
#184.5 (only provider transports substituted), #184.6 (pageerror/console
collectors; CI sets ``VERTEP_REQUIRE_MEDIA_BROWSER_E2E=1`` so a missing
requirement fails instead of skipping).
"""

from __future__ import annotations

import base64
import os
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from core.app import app
from tests import media_harness as harness

ROOT = Path(__file__).resolve().parents[1]
WORKER_TOKEN = "media-browser-worker-token"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "vertep-test-admin-secret")
REQUIRE = os.getenv("VERTEP_REQUIRE_MEDIA_BROWSER_E2E") == "1"

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - dependency is required in CI
    if REQUIRE:
        pytest.fail("playwright is not installed; media Browser proof is required",
                    pytrace=False)
    pytest.skip("playwright is not installed", allow_module_level=True)


def _unsatisfied(message: str):
    if REQUIRE:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _web_dist() -> Path:
    for candidate in (ROOT / "web-v2/dist/vertep-admin-v2/browser",
                      ROOT / "web-v2/dist/vertep-admin-v2"):
        if (candidate / "index.html").is_file():
            return candidate
        if (candidate / "browser/index.html").is_file():
            return candidate / "browser"
    _unsatisfied("web-v2 production build is missing; run `npm run build` in web-v2")
    raise AssertionError("unreachable")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until(predicate, timeout=60, interval=0.2, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"Timed out waiting for {message}")


@pytest.fixture
def core(tmp_path, monkeypatch):
    """Disposable CORE on a loopback socket with controlled provider transports."""
    _web_dist()
    monkeypatch.setenv("NODE_API_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    failures: list[BaseException] = []

    def _serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # pragma: no cover - surfaced as a failure
            failures.append(exc)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    auth = "Basic " + base64.b64encode(
        f"admin:{ADMIN_PASSWORD}".encode("utf-8")).decode("ascii")

    seen: dict[str, str] = {}

    def _ready() -> bool:
        if failures:
            pytest.fail(f"CORE server failed to start: {failures[0]!r}")
        ok, detail = _health_probe(base)
        if not ok and seen.get("detail") != detail:
            seen["detail"] = detail
            print(f"CORE health not ready: {detail}")
        return ok

    try:
        _wait_until(_ready, timeout=60, message="CORE to become healthy")
        with httpx.Client(base_url=base, headers={
                    "Authorization": auth, "x-vertep-token": WORKER_TOKEN},
                          timeout=30) as api:
            harness.reset_counters()
            harness.register_fleet(
                api, characters_root=tmp_path / "characters",
                character_id="browserchar", monkeypatch=monkeypatch)
            listed = api.get("/api/characters")
            listed.raise_for_status()
            character_id = listed.json()[0]["id"]
            yield {"url": base, "api": api, "auth": auth, "characters": tmp_path,
                   "character_id": character_id, "monkeypatch": monkeypatch}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _health_probe(base: str) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{base}/api/health", timeout=15)
        return response.status_code == 200, f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.HTTPError as exc:
        return False, repr(exc)


def _healthy(base: str) -> bool:
    return _health_probe(base)[0]


@pytest.fixture
def browser(core):
    """Authenticated Chromium context with pageerror/console collectors."""
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        context = instance.new_context(base_url=core["url"])
        context.set_default_timeout(20_000)
        response = context.request.post(
            f"{core['url']}/api/session",
            headers={"Authorization": core["auth"]})
        assert response.ok, response.text()
        page = context.new_page()
        page_errors: list[str] = []
        console_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        yield {"page": page, "page_errors": page_errors,
               "console_errors": console_errors, "url": core["url"]}
        assert page_errors == [], f"uncaught page errors: {page_errors}"
        assert console_errors == [], f"console errors: {console_errors}"
        context.close()
        instance.close()


def _open_job(browser, job_id: str):
    page = browser["page"]
    page.goto(f"{browser['url']}/jobs/{job_id}")
    expect(page.get_by_test_id("job-detail-page")).to_be_visible()
    return page


def _approve_dialog(page):
    dialog = page.get_by_role("dialog").last
    expect(dialog).to_be_visible()
    dialog.get_by_role("button").last.click()


def _script_and_storyboard_ready(api, monkeypatch, character_id: str) -> str:
    job_id = harness.create_job(api, topic="Browser storyboard",
                                character_id=character_id)
    harness.approve_script_only(api, job_id, monkeypatch)
    harness.run_storyboard_task(api, job_id, monkeypatch)
    harness.wait_for_status(api, job_id, "STORYBOARD_PENDING_APPROVAL")
    harness.submit_storyboard_previews(api, job_id, count=2)
    harness.wait_for_status(api, job_id, "STORYBOARD_READY")
    return job_id


def test_browser_job_creation_and_script_review_are_persisted(core, browser):
    """#184.4: create and review the script through the Web UI, read it back from CORE."""
    page = browser["page"]
    api = core["api"]
    page.goto(f"{browser['url']}/jobs")
    expect(page.get_by_test_id("jobs-page")).to_be_visible()
    page.get_by_test_id("create-job-button").click()
    modal = page.get_by_test_id("create-job-modal")
    expect(modal).to_be_visible()
    modal.get_by_test_id("job-topic-input").fill("Browser created job")
    modal.locator("select").first.select_option(core["character_id"])
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/api/jobs")) as created:
        modal.get_by_role("button").last.click()
    assert created.value.ok, created.value.text()
    job_id = created.value.json()["job_id"]
    expect(modal).to_be_hidden()

    job = harness.get_job(api, job_id)
    assert job["topic"] == "Browser created job"
    assert job["character_id"] == core["character_id"]

    harness.run_script_task(api, job_id)
    harness.wait_for_status(api, job_id, "SCRIPT_PENDING_APPROVAL")
    _open_job(browser, job_id)
    expect(page.get_by_test_id("job-script")).to_be_visible()

    revision = "Додай більше деталей у сценарій"
    page.once("dialog", lambda dialog: dialog.accept(revision))
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/script/revision")) as revised:
        page.get_by_test_id("revision-script-button").click()
    assert revised.value.ok, revised.value.text()
    assert harness.get_job(api, job_id)["revision"] == revision
    regenerated = api.post(f"/api/jobs/{job_id}/script/regenerate",
                           json={"revision": revision, "actor": "harness"})
    assert regenerated.status_code == 200, regenerated.text
    harness.wait_for_status(api, job_id, "SCRIPT_QUEUED")
    harness.run_script_task(api, job_id, expect_revision=revision)
    harness.wait_for_status(api, job_id, "SCRIPT_PENDING_APPROVAL")

    page.reload()
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/script/approve")) as approved:
        page.get_by_test_id("approve-script-button").click()
    assert approved.value.ok, approved.value.text()
    harness.wait_for_status(api, job_id, "STORYBOARD_QUEUED")

    harness.run_storyboard_task(api, job_id, core["monkeypatch"])
    harness.wait_for_status(api, job_id, "STORYBOARD_PENDING_APPROVAL")
    harness.submit_storyboard_previews(api, job_id, count=2)
    ready = harness.wait_for_status(api, job_id, "STORYBOARD_READY")
    assert ready["active_storyboard_version"] == 1


def test_browser_storyboard_history_revision_regenerate_and_stale_conflict(
        core, browser):
    """#184.2: scene/all image mutations, version history and a stale 409 in the UI."""
    page = browser["page"]
    api = core["api"]
    job_id = _script_and_storyboard_ready(api, core["monkeypatch"], core["character_id"])
    _open_job(browser, job_id)
    storyboard = page.get_by_test_id("job-storyboard")
    expect(storyboard).to_be_visible()
    assert storyboard.get_by_test_id("scene-preview-image").count() == 2
    expect(page.get_by_test_id("storyboard-version-select")).to_be_visible()

    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/storyboards/images/revision")) as scene:
        storyboard.get_by_role("button", name="Перегенерувати сцену").first.click()
    assert scene.value.ok, scene.value.text()
    assert scene.value.request.post_data_json["scene_indexes"] == [1]
    board = harness.active_storyboard(api, job_id)
    assert board["image_version"] == 2
    harness.submit_storyboard_previews(api, job_id, count=1)
    _wait_until(lambda: harness.active_storyboard(api, job_id)["image_status"] == "ready",
                message="scene preview to become ready")

    page.reload()
    storyboard = page.get_by_test_id("job-storyboard")
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/storyboards/images/regenerate")) as all_images:
        storyboard.get_by_role("button", name=re.compile("Перегенерувати всі")).click()
    assert all_images.value.ok, all_images.value.text()
    board = harness.active_storyboard(api, job_id)
    assert board["image_version"] == 3
    harness.submit_storyboard_previews(api, job_id, count=2)
    _wait_until(lambda: harness.active_storyboard(api, job_id)["image_status"] == "ready",
                message="regenerated previews to become ready")

    page.reload()
    revision = "Переробити композицію кадрів"
    page.once("dialog", lambda dialog: dialog.accept(revision))
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/storyboards/regenerate")) as storyboard_revision:
        page.get_by_test_id("revision-job-button").click()
    assert storyboard_revision.value.ok, storyboard_revision.value.text()
    harness.run_storyboard_task(api, job_id, core["monkeypatch"], expect_revision=revision)
    harness.wait_for_status(api, job_id, "STORYBOARD_PENDING_APPROVAL")
    harness.submit_storyboard_previews(api, job_id, count=2)
    board = harness.wait_for_status(api, job_id, "STORYBOARD_READY")
    assert board["active_storyboard_version"] == 2

    page.reload()
    select = page.get_by_test_id("storyboard-version-select")
    select.select_option("1")
    expect(page.get_by_test_id("job-storyboard")).not_to_contain_text("[sb-rev:")
    select.select_option("2")
    expect(page.get_by_test_id("job-storyboard")).to_contain_text("[sb-rev:")

    current = harness.active_storyboard(api, job_id)
    api.post(f"/api/jobs/{job_id}/storyboards/images/revision", json={
        "version": current["version"], "actor": "harness",
        "image_version": current["image_version"]})
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/storyboards/images/approve")) as stale:
        page.get_by_test_id("job-storyboard").get_by_role(
            "button", name=re.compile("Затвердити прев")).click()
    assert stale.value.status == 409
    expect(page.get_by_test_id("job-conflict-banner")).to_be_visible()
    browser["console_errors"][:] = [
        message for message in browser["console_errors"] if "status of 409" not in message]

    harness.submit_storyboard_previews(api, job_id, count=2)
    _wait_until(lambda: harness.active_storyboard(api, job_id)["image_status"] == "ready",
                message="post-conflict previews to become ready")
    page.reload()
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/storyboards/images/approve")) as image_ok:
        page.get_by_test_id("job-storyboard").get_by_role(
            "button", name=re.compile("Затвердити прев")).click()
    assert image_ok.value.ok, image_ok.value.text()
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/storyboards/approve")) as final_ok:
        page.get_by_test_id("approve-final-button").click()
    assert final_ok.value.ok, final_ok.value.text()
    harness.wait_for_status(api, job_id, "ASSET_GENERATION")


def test_browser_video_review_and_publication_are_persisted(core, browser):
    """#184.4: video revision/regenerate/approve and publication through the UI."""
    page = browser["page"]
    api = core["api"]
    job_id = harness.create_job(api, topic="Browser video", character_id="browserchar")
    harness.approve_script(api, job_id, core["monkeypatch"])
    harness.generate_assets(api, job_id, core["monkeypatch"])
    harness.wait_for_video(api, job_id, min_version=1)

    _open_job(browser, job_id)
    expect(page.get_by_test_id("video-approval-section")).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept("regenerate"))
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/video/revision")) as revision:
        page.get_by_test_id("revision-video-button").first.click()
    assert revision.value.ok, revision.value.text()
    assert harness.get_job(api, job_id)["status"] == "VIDEO_REVISION_REQUESTED"

    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/video/regenerate")) as regenerate:
        page.get_by_test_id("regenerate-video-button").first.click()
        _approve_dialog(page)
    assert regenerate.value.ok, regenerate.value.text()
    harness.wait_for_video(api, job_id, min_version=2)

    page.reload()
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/video/approve")) as approved:
        page.get_by_test_id("approve-video-button").first.click()
    assert approved.value.ok, approved.value.text()
    harness.wait_for_status(api, job_id, "READY")

    harness.register_publisher(api)
    page.reload()
    with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/publish")) as published:
        page.get_by_test_id("publish-confirm-button").click()
    assert published.value.ok, published.value.text()
    task = harness.claim_publish(api, job_id, channel="youtube")
    harness.submit_publish_receipt(
        api, job_id, task, harness.sandbox_receipt("youtube", task))
    harness.wait_for_status(api, job_id, "PUBLISHED")

    page.reload()
    results = page.get_by_test_id("publication-results")
    expect(results).to_be_visible()
    expect(results).to_contain_text("YouTube")
    job = harness.get_job(api, job_id)
    assert job["publication_results"]["youtube"]["status"] == "PUBLISHED"
