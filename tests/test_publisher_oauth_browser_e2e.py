"""#361: actual settings UI/API reconnect against a controlled OAuth server."""
import json
import time

from tests.test_media_browser_e2e import core, browser, expect
from tests.test_publisher_oauth_state import oauth
from tests.acceptance_provenance import acceptance_provenance
from core.first_run import ensure_secret_store, _write_encrypted_secrets


def test_publisher_test_reconnect_scope_error_and_secret_redaction(core, browser, oauth, tmp_path):
    page = browser["page"]
    page.goto(core["url"] + "/settings?tab=integrations")
    panel = page.get_by_test_id("settings-integrations")
    expect(panel).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith("/api/integrations") and r.request.method == "GET") as checked:
        panel.get_by_test_id("publisher-test").click()
    assert checked.value.status == 200
    states = []
    def reconnect():
        with page.expect_response(lambda r: r.url.endswith("/api/settings/publisher/youtube/reconnect")) as response:
            panel.get_by_test_id("publisher-reconnect-youtube").click()
        data = response.value.json()
        assert response.value.status == 200
        states.append(data)
        return data
    data = reconnect()
    assert data["reconnected"] and data["ready"]
    expect(panel.get_by_test_id("publisher-status-youtube")).to_contain_text("Підключення відновлено")
    assert ensure_secret_store()["youtube_access_token"]
    oauth["scope"] = "unrelated.scope"
    data = reconnect()
    assert data["reconnected"] and not data["ready"] and data["missing_scopes"]
    expect(panel.get_by_test_id("publisher-status-youtube")).to_contain_text("Бракує прав доступу")
    oauth["fail"] = True
    stored = ensure_secret_store()
    stored["publisher_oauth"]["YOUTUBE_ACCESS_TOKEN"]["expires_at"] = time.time() - 1
    _write_encrypted_secrets(stored)
    data = reconnect()
    assert not data["reconnected"] and not data["ready"]
    expect(panel.get_by_test_id("publisher-status-youtube")).to_contain_text("Підключення не готове")
    visible = page.content() + json.dumps(states) + checked.value.text()
    for value in ["synthetic-access-", "synthetic-refresh", "synthetic-client-secret"]:
        assert value not in visible
    assert oauth["calls"]
    page.screenshot(path=str(tmp_path / "publisher-oauth.png"), full_page=True)
    (tmp_path / "publisher-oauth-evidence.json").write_text(json.dumps({"api": states, "oauth_calls": oauth["calls"]}))
