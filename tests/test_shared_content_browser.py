"""#500 AC3/AC5/AC6/AC8 in the Browser against a live CORE (no API stubs).

The owner brand's moderator reviews the exact version/hash from «Спільний
контент», approves it, creates its own publication with its own texts and
cover, and starts sending it to its own channel from the job page. The
Publisher Worker then receives exactly that artifact, those texts and the
owner brand's channel; the source material is not cross-posted.
"""

import hashlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from core.models import Channel, JobStatus, VideoVersion

WORKER = {"X-Vertep-Token": "worker-token"}


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_core(tmp_path, monkeypatch):
    from core.first_run import password_hash
    brands = tmp_path / "brands"
    for brand in ("brand-a", "brand-b"):
        (brands / brand).mkdir(parents=True)
        (brands / brand / "brand.json").write_text(json.dumps({"id": brand, "name": f"Бренд {brand[-1].upper()}"}))
    monkeypatch.setenv("BRANDS_ROOT", str(brands))
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "100000")
    monkeypatch.setenv("USERS_JSON", json.dumps({
        "mod-a": {"password_hash": password_hash("mod-a-password-123"), "role": "moderator", "brands": ["brand-a"]},
        "mod-b": {"password_hash": password_hash("mod-b-password-123"), "role": "moderator", "brands": ["brand-b"]}}))
    from core.app import app
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/api/health", timeout=2).status_code < 500:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def test_owner_reviews_the_exact_version_and_publishes_its_own_copy_to_its_channel(live_core):
    from core.state import store, task_queue
    from tests.test_media_browser_e2e import sync_playwright, expect
    url = live_core
    store.repository.save_channel(Channel(channel_id="ch-b-telegram", brand_id="brand-b", channel_type="telegram",
                                          target="@b_news"))
    job = store.create("Матеріал бренду A", "hero", 5, "web", "image", 0, "brand-a")
    video = store.root / job.job_id / "final" / "video-v1.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"approved material v1")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    with store.lock:
        job.character_owner_brand = "brand-b"
        job.video_versions = [VideoVersion(version=1, path="final/video-v1.mp4", sha256=digest, approved=True)]
        job.active_video_version = 1
        job.output_path = str(video)
        store.update(job, JobStatus.READY, "READY (material of brand A)")
    httpx.post(url + "/api/workers/heartbeat", headers=WORKER, timeout=10, json={
        "node_name": "pub-worker", "vram_mb": 0, "role": "publisher",
        "capabilities": ["publishing"], "supported_tasks": ["publish"]})
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                import base64
                auth = "Basic " + base64.b64encode(b"mod-b:mod-b-password-123").decode()
                assert context.request.post(url + "/api/session", headers={"Authorization": auth}).status == 200
                page.goto(url + "/brands")
                item = page.get_by_test_id(f"shared-{job.job_id}")
                expect(item).to_contain_text("Очікує погодження", timeout=20000)
                review = page.get_by_test_id(f"shared-view-{job.job_id}").get_attribute("href")
                reviewed = context.request.get(url + review)
                assert reviewed.status == 200 and hashlib.sha256(reviewed.body()).hexdigest() == digest
                assert context.request.get(url + review.replace(digest, "0" * 64)).status == 409
                item.get_by_text("Погодити", exact=True).click()
                expect(item).to_contain_text("Погоджено")
                page.get_by_test_id(f"own-publication-{job.job_id}").click()
                page.get_by_test_id("own-title").fill("Версія бренду B")
                page.get_by_test_id("own-description").fill("Опис для каналу B")
                page.get_by_test_id("own-hashtags").fill("#b, #news")
                page.get_by_test_id("own-cover").fill("https://cdn.example/b.jpg")
                page.get_by_test_id("own-create").click()
                page.wait_for_url(lambda target: "/jobs/" in target and job.job_id not in target, timeout=20000)
                derived_id = page.url.rstrip("/").split("/")[-1]
                page.locator("label", has_text="Telegram").locator("input[type=checkbox]").first.check(timeout=20000)
                page.get_by_test_id("publish-confirm-button").click()
                expect(page.get_by_text("Публікацію запущено")).to_be_visible(timeout=20000)
                assert errors == []
            finally:
                browser.close()
        derived = store.jobs[derived_id]
        assert derived.brand_id == "brand-b" and derived.shared_source["sha256"] == digest
        task = None
        for _ in range(50):
            task = httpx.post(url + "/api/tasks/claim", headers=WORKER, timeout=10,
                              json={"node_name": "pub-worker", "vram_mb": 0}).json().get("task")
            if task and task.get("job_id") == derived_id:
                break
        assert task and task["channel"] == "telegram"
        assert task["publish_intent"]["target"] == "@b_news" and task["publish_intent"]["video_sha256"] == digest
        assert task["metadata"]["description"] == "Опис для каналу B"
        assert task["metadata"]["hashtags"] == ["#b", "#news"] and task["metadata"]["cover"] == "https://cdn.example/b.jpg"
        assert task["metadata"]["brand_id"] == "brand-b" and task["topic"] == "Версія бренду B"
        assert not store.jobs[job.job_id].publish_task_ids  # the source material is not cross-posted
        task_queue.ack(task["task_id"])
    finally:
        store.repository.delete_channel("ch-b-telegram")
