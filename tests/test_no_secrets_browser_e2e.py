"""#356: the existing synthetic corpus through HTTP ingestion, disk and DOM.

Surface trace: test_no_secrets_acceptance::test_secret_corpus_never_crosses_real_surfaces
covers logs, Telegram transport, Runner report, audit and error response using
the same SECRET_CORPUS. This test supplies the missing publication boundary.
"""
import json

from tests.test_no_secrets_acceptance import SECRET_CORPUS
from tests.acceptance_provenance import acceptance_provenance
from tests.test_media_browser_e2e import core, browser, _open_job, expect, harness


def test_corpus_http_persistence_and_dom(core, browser, tmp_path):
    def clean(value):
        blob = value if isinstance(value, str) else json.dumps(value)
        for secret in SECRET_CORPUS.values():
            assert secret not in blob

    api = core["api"]
    job_id = harness.create_job(api, topic="No-secrets corpus", character_id="browserchar")
    harness.approve_script(api, job_id, core["monkeypatch"])
    harness.generate_assets(api, job_id, core["monkeypatch"])
    harness.wait_for_video(api, job_id, min_version=1)
    harness.approve_video(api, job_id)
    harness.register_publisher(api)
    response = api.post(f"/api/jobs/{job_id}/publish", json=["youtube"])
    assert response.status_code == 200, response.text
    task = harness.claim_publish(api, job_id, channel="youtube")
    receipt = harness.sandbox_receipt("youtube", task)
    receipt.update(SECRET_CORPUS)
    receipt["error"] = json.dumps(SECRET_CORPUS)
    receipt["url"] = "https://example.invalid/benign?token=" + SECRET_CORPUS["token"]
    receipt["note"] = "benign-corpus-marker"
    artifact = harness.role_executor._artifact(
        "publication.json", "publication_receipt", json.dumps(receipt).encode())
    response = api.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"],
        "node_name": harness.PUBLISHER_NODE, "success": True, "artifacts": [artifact]})
    assert response.status_code == 200, response.text
    clean(response.text)
    harness.wait_for_status(api, job_id, "PUBLISHED")

    response = api.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    clean(response.text)
    result = response.json()["publication_results"]["youtube"]
    assert result["note"] == "benign-corpus-marker"
    assert result["status"] == "PUBLISHED"
    # Reload from the actual repository, rather than checking the live Job.
    persisted = harness.restart_store().jobs[job_id]
    clean(persisted.publication_results)
    assert persisted.publication_results["youtube"]["note"] == "benign-corpus-marker"
    for path in harness.job_root(job_id).rglob("*.json"):
        clean(path.read_text(encoding="utf-8"))
    page = _open_job(browser, job_id)
    results = page.get_by_test_id("publication-results")
    expect(results).to_be_visible()
    expect(results).to_contain_text("PUBLISHED")
    expect(results).to_contain_text("YouTube")
    expect(results).to_contain_text("[REDACTED]")
    clean(page.content())
    page.screenshot(path=str(tmp_path / "no-secrets-dom.png"), full_page=True)
    (tmp_path / "no-secrets-evidence.json").write_text(json.dumps({
        "http_job": response.json(), "persisted_receipt": persisted.publication_results,
        "surface_trace": "test_no_secrets_acceptance::test_secret_corpus_never_crosses_real_surfaces",
        "corpus_keys": list(SECRET_CORPUS), "dom": results.inner_text(),
    }, indent=2), encoding="utf-8")
