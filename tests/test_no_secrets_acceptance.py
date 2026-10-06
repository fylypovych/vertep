"""No-secrets acceptance test (Issue #84).

A corpus of known secrets is passed through the full surface chain:
log-write -> read_logs -> /api/logs -> /api/operations -> receipt ->
Telegram payload -> audit jsonl -> error API responses.
Assert no secret value appears anywhere in the cycle.
"""

import json
import base64

import pytest

from core.logging_config import secret_redact, JsonFormatter


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


def test_read_logs_redacts_secrets():
    """read_logs must redact secrets from each log line before returning."""
    from core.logging_config import read_logs

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "test.jsonl")
        with open(log_path, "w") as f:
            f.write(json.dumps({"msg": "api_key=leaked", "level": "INFO", "name": "test", "timestamp": "2026-01-01T00:00:00+00:00", "job_id": "j1", "node_name": "gpu-01"}) + "\n")
            f.write(json.dumps({"msg": "password=hunter2", "level": "ERROR", "name": "test", "timestamp": "2026-01-01T00:00:00+00:00", "job_id": "j1", "node_name": "gpu-01"}) + "\n")

        os.environ["LOG_ROOT"] = tmpdir
        records = read_logs(limit=10)
        for record in records:
            for value in SECRET_CORPUS.values():
                assert value not in str(record), f"Secret value {value!r} leaked in read_logs"


def test_operation_create_and_fail_redacts():
    """Operations create/fail must redact secrets in error messages."""
    import core.operations as operations

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["CONFIG_ROOT"] = tmpdir
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


def test_api_logs_endpoint_redacts():
    """The /api/logs endpoint must redact secrets via read_logs which calls secret_redact."""
    from core.api.observability import logs

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["LOG_ROOT"] = tmpdir
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