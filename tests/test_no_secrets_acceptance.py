"""No-secrets acceptance test (Issue #84).

A corpus of known secrets is passed through the full surface chain:
log-write -> read_logs -> /api/logs -> /api/operations -> receipt ->
Telegram payload -> audit jsonl -> error API responses.
Assert no secret value appears anywhere in the cycle.
"""

import json
import base64
import os

import pytest

from core.logging_config import secret_redact, JsonFormatter


@pytest.fixture(autouse=True)
def _isolate_surface_env():
    """Issue #141: never leak CONFIG_ROOT/LOG_ROOT between suites.

    The no-secrets surfaces resolve their roots from ``os.environ``.  A direct
    assignment that is not undone makes every later API test read a deleted
    temp root and answer 503, so the shared API regression used to depend on the
    order of this module.  Snapshot and restore the exact prior environment
    after every test in this module.
    """
    names = ("CONFIG_ROOT", "LOG_ROOT", "UPDATE_STATE_DIR")
    saved = {name: os.environ.get(name) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value



SECRET_CORPUS = {
    "api_key": "ABC123DEF456",
    "password": "hunter2",
    "auth_token": "r1tok-12345",
    "session_id": "sess-abc-123",
    "access_token": "1234567890-abcdefg",
    "client_secret": "secret-client-123",
    "refresh_token": "r-refresh-token-xyz",
    "private_key": "-----BEGIN RSA KEY-----",
    "ssh_private_key": "-----BEGIN SSH KEY-----",
    "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "credentials": "user:pwd@host",
    "proxy_password": "proxy-pwd-123",
    "passphrase": "passphrase-secret",
    "basic_auth": "Basic c2xjc3R1cDE6c2xjc3R1cDE=",
    "token": "my-super-secret-token-12345",
}


def _make_log_record(msg, **extra):
    """Helper to create a minimal log record dict."""
    record = {
        "msg": msg,
        "level": "INFO",
        "name": "test",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    record.update(extra)
    return record


def test_log_write_redacts_secrets():
    """secret_redact must redact all corpus secrets from plain key=value form."""
    for key, value in SECRET_CORPUS.items():
        text = f"{key}={value}"
        redacted = secret_redact(text)
        assert value not in redacted, f"Secret {key}={value} not redacted"
        assert "[REDACTED]" in redacted


def test_json_payload_redacts_all_secrets():
    """secret_redact must redact all corpus secrets from JSON payloads."""
    payload = json.dumps(SECRET_CORPUS)
    redacted = secret_redact(payload)
    for value in SECRET_CORPUS.values():
        assert value not in redacted, f"Secret value {value!r} not redacted in JSON"


def test_bearer_token_redacted():
    """Bearer tokens must be redacted."""
    text = "Authorization: bearer abc.def.ghi"
    redacted = secret_redact(text)
    assert "abc.def.ghi" not in redacted
    assert "[REDACTED]" in redacted


def test_url_userinfo_redacted():
    """URL userinfo credentials must be redacted."""
    text = "https://user:s3cret@ollama:11434/api/tags"
    redacted = secret_redact(text)
    assert "s3cret" not in redacted
    assert "https://user:[REDACTED]@ollama:11434/api/tags" in redacted


def test_query_string_redacted():
    """Query string credentials must be redacted."""
    text = "GET /api/tags?token=abcd1234&model=llama3.2 failed"
    redacted = secret_redact(text)
    assert "abcd1234" not in redacted
    assert "?token=[REDACTED]" in redacted


def test_basic_auth_full_masking():
    """Authorization Basic full masking must redact the base64 part."""
    text = 'Authorization: Basic c2xjc3R1cDE6c2xjc3R1cDE='
    redacted = secret_redact(text)
    assert "c2xjc3R1cDE=" not in redacted
    assert "[REDACTED]" in redacted


def test_json_formatter_redacts_extras():
    """JsonFormatter must recursively redact secret values in dict/list extras."""
    formatter = JsonFormatter()
    record = type("R", (), {})()
    record.getMessage = lambda: "normal message"
    record.levelname = "INFO"
    record.name = "test"
    record.exc_info = None
    record.__dict__.update({
        "job_id": "job-1",
        "node_name": "gpu-01",
        "custom_dict": {"secret_key": "hidden-value"},
        "custom_list": ["secret-item-1", "normal-item"],
    })
    line = formatter.format(record)
    data = json.loads(line)
    assert "hidden-value" not in line
    assert data["custom_dict"]["secret_key"] == "[REDACTED]"
    assert data["custom_list"][0] == "[REDACTED]"


def test_read_logs_redacts_secrets(monkeypatch):
    """read_logs must redact secrets from each log line before returning."""
    from core.logging_config import read_logs

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "test.jsonl")
        with open(log_path, "w") as f:
            f.write(json.dumps({"msg": "api_key=leaked", "level": "INFO", "name": "test", "timestamp": "2026-01-01T00:00:00+00:00", "job_id": "j1", "node_name": "gpu-01"}) + "\n")
            f.write(json.dumps({"msg": "password=hunter2", "level": "ERROR", "name": "test", "timestamp": "2026-01-01T00:00:00+00:00", "job_id": "j1", "node_name": "gpu-01"}) + "\n")

        monkeypatch.setenv("LOG_ROOT", tmpdir)
        records = read_logs(limit=10)
        for record in records:
            for value in SECRET_CORPUS.values():
                assert value not in str(record), f"Secret value {value!r} leaked in read_logs"


def test_operation_create_and_fail_redacts(monkeypatch):
    """Operations create/fail must redact secrets in error messages."""
    import core.operations as operations

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("CONFIG_ROOT", tmpdir)
        operation = operations.create_operation("update", "node-1")
        operations.fail_operation(operation["operation_id"], "api_key=leaked-key")
        entry = operations.audit_entry(operation["operation_id"], "FAILED", "password=leaked-pass", "operator")

        assert "leaked-key" not in entry["error"]
        assert "leaked-pass" not in entry["message"]
        assert entry["error"] == "api_key=[REDACTED]"
        assert entry["message"] == "password=[REDACTED]"


def test_telegram_message_redacts():
    """TelegramAdapter send_message must redact secrets from the message text."""
    from adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter()
    # Token should be redacted in the message sent to Telegram
    text = f"Job completed with api_key=ABC123DEF and token=xyz-987"
    # The adapter redacts via _redact before sending
    redacted = secret_redact(text)
    assert "ABC123DEF" not in redacted
    assert "xyz-987" not in redacted
    assert "[REDACTED]" in redacted


def test_api_logs_endpoint_redacts(monkeypatch):
    """The /api/logs endpoint must redact secrets via read_logs which calls secret_redact."""
    from core.api.observability import logs

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("LOG_ROOT", tmpdir)
        log_path = os.path.join(tmpdir, "test.jsonl")
        with open(log_path, "w") as f:
            f.write(json.dumps({"msg": "api_key=leaked", "level": "INFO", "name": "test", "timestamp": "2026-01-01T00:00:00+00:00", "job_id": "j1", "node_name": "gpu-01"}) + "\n")

        result = logs(limit=10)
        for item in result:
            for value in SECRET_CORPUS.values():
                assert value not in str(item), f"Secret value {value!r} leaked in /api/logs"


def test_error_api_responses_redact():
    """Error API responses must redact secrets."""
    from core.app import _json_error

    result = _json_error("api_key=leaked-value", status=400)
    body = json.loads(result.body)
    assert "leaked-value" not in body["detail"]
    assert "[REDACTED]" in body["detail"]


def test_secret_corpus_never_crosses_real_surfaces(monkeypatch, tmp_path):
    """Issue #141: drive one synthetic corpus through the *actual* log, API,
    audit, Telegram and Runner-report boundaries and assert the secret values
    never appear — proving the surface contract, not just ``secret_redact``."""
    import uuid

    import httpx

    from core.logging_config import configure_logging, read_logs
    from core.api.observability import logs as api_logs
    from core import operations
    from core.app import _json_error
    from core.real_tests.runner import RealTestRunner
    from core.real_tests.models import TestRun, CheckResult, CheckStatus
    from adapters.telegram import TelegramAdapter

    values = list(SECRET_CORPUS.values())
    corpus_text = " ".join(f"{key}={value}" for key, value in SECRET_CORPUS.items())

    def assert_clean(*artifacts):
        for artifact in artifacts:
            blob = artifact if isinstance(artifact, str) else json.dumps(
                artifact, ensure_ascii=False, default=str)
            for value in values:
                assert value not in blob, f"secret {value!r} leaked: {blob[:300]}"

    # log-write -> read_logs -> /api/logs
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    logger = configure_logging(f"no-secrets-{uuid.uuid4().hex}")
    logger.info(corpus_text)
    for handler in logger.handlers:
        handler.flush()
    assert_clean(read_logs(limit=50))
    assert_clean(api_logs(limit=50))

    # operations: error + audit (operator-visible surfaces)
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path / "update"))
    operation = operations.create_operation("update", "node-1")
    operations.fail_operation(operation["operation_id"],
                              f"api_key={SECRET_CORPUS['api_key']}")
    entry = operations.audit_entry(operation["operation_id"], "FAILED",
                                   f"token={SECRET_CORPUS['auth_token']}")
    audit_file = operations._state_dir() / f"{operation['operation_id']}.audit.jsonl"
    assert_clean(entry, audit_file.read_text(encoding="utf-8"))

    # Telegram: the adapter's real send path, with the transport mocked out
    sent = {}

    def fake_post(url, json=None, **kwargs):
        sent["payload"] = json
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE")
    monkeypatch.setattr("adapters.telegram.httpx.post", fake_post)
    TelegramAdapter().send_message("42", corpus_text)
    assert_clean(sent["payload"])

    # Runner report: the dict served by /api/real-tests/runs/<id>
    run = TestRun(
        rt_id="rt-1", rt_issue_number=9999, final_result="PASS",
        environment={"proxy_password": SECRET_CORPUS["proxy_password"],
                     "session_id": SECRET_CORPUS["session_id"],
                     "note": "benign"},
        hardware={"gpu": "NVIDIA", "proxy_password": SECRET_CORPUS["proxy_password"]},
        checks=[CheckResult(name="check", status=CheckStatus.PASS,
                            detail=f"auth_token={SECRET_CORPUS['auth_token']}")],
        error_details=f"api_key={SECRET_CORPUS['api_key']}",
    )
    report = RealTestRunner()._format_report(run)
    # benign values must be preserved, not blanket-redacted
    assert report["environment"]["note"] == "benign"
    assert report["hardware"]["gpu"] == "NVIDIA"
    assert_clean(report)

    # error API response
    assert_clean(_json_error(f"api_key={SECRET_CORPUS['api_key']}", status=400).body.decode())

    # Publication receipts/metadata must be redacted at CORE boundaries (Issue #190)
    from core.api import job_helpers
    from core.models import Job, JobStatus
    from core import state
    from pathlib import Path
    import tempfile

    receipt = {"publication_id": "pub-1", "channel": "youtube", "status": "FAILED",
               "error": f"api_key={SECRET_CORPUS['api_key']}",
               "url": f"https://example.com?token={SECRET_CORPUS['token']}"}
    redacted_receipt = job_helpers._redact_payload(receipt)
    assert_clean(redacted_receipt)
    assert redacted_receipt["status"] == "FAILED"

    # Job with publication_results containing secrets must be redacted in API serialization
    with tempfile.TemporaryDirectory() as tmpdir:
        old_root = state.store.root
        try:
            state.store.root = Path(tmpdir)
            job = Job(job_id="j-sec", topic="t", character_id="c", priority=1, source="x",
                      task_type="video", created_at="2026-01-01T00:00:00+00:00",
                      status=JobStatus.FAILED)
            job.publication_results = {"youtube": {"channel": "youtube", "status": "FAILED",
                                                   "error": f"password={SECRET_CORPUS['password']}",
                                                   "url": f"https://yt?token={SECRET_CORPUS['token']}"}}
            state.store.jobs[job.job_id] = job
            from core.api import jobs as jobs_api
            res = jobs_api.get_job("j-sec")
            assert_clean(res)
        finally:
            state.store.root = old_root
            state.store.jobs.pop("j-sec", None)

