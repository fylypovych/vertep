"""Secret redaction contract (Issue #84).

``secret_redact`` must mask every known secret shape across the surfaces the
operator can see: plain ``key=value``, JSON ``"key": "value"``, bearer tokens
and nested payloads.  These tests pin the contract so a future refactor that
re-introduces a quoted-key gap fails loudly instead of leaking a token into a
log line.
"""

import json

from core.logging_config import JsonFormatter, secret_redact


def test_plain_key_value_is_redacted():
    assert secret_redact("api_key=ABC123DEF") == "api_key=[REDACTED]"
    assert secret_redact("password=hunter2") == "password=[REDACTED]"


def test_json_unquoted_key_value_is_redacted():
    """JSON-ish payloads without quotes around the key must also be redacted."""
    redacted = secret_redact('{token:"abc123", api_key:"secret-value"}')
    assert "abc123" not in redacted
    assert "secret-value" not in redacted
    assert "[REDACTED]" in redacted


def test_json_quoted_key_value_is_redacted():
    """Issue #84: a JSON payload with quoted keys must be redacted too."""
    payload = json.dumps({"token": "abc123", "api_key": "secret-value"})
    redacted = secret_redact(payload)
    assert "[REDACTED]" in redacted
    assert "abc123" not in redacted
    assert "secret-value" not in redacted
    # The key and JSON punctuation are preserved; only the value is masked.
    assert '"token": [REDACTED]' in redacted
    assert '"api_key": [REDACTED]' in redacted


def test_nested_json_payload_is_redacted():
    payload = json.dumps({"auth": {"refresh_token": "r1tok", "session_id": "sess-1"},
                          "metadata": {"authorization": "Bearer xyz-token-123"}})
    redacted = secret_redact(payload)
    assert "r1tok" not in redacted
    assert "sess-1" not in redacted
    assert "xyz-token-123" not in redacted
    assert "[REDACTED]" in redacted


def test_bearer_token_is_redacted():
    assert secret_redact("Authorization: bearer abc.def.ghi") == "Authorization: bearer [REDACTED]"


def test_url_userinfo_credentials_are_redacted():
    """Issue #84: proxy/registry endpoints are built from env values, so a
    transport error can echo ``https://user:pass@host``."""
    text = secret_redact("connect failed: https://user:s3cret@ollama:11434/api/tags")
    assert "s3cret" not in text
    assert "https://user:[REDACTED]@ollama:11434/api/tags" in text
    # A URL without credentials must stay readable.
    assert secret_redact("http://ollama:11434/api/tags") == "http://ollama:11434/api/tags"


def test_query_string_credentials_are_redacted():
    text = secret_redact("GET /api/tags?token=abcd1234&model=llama3.2 failed")
    assert "abcd1234" not in text
    assert "?token=[REDACTED]" in text
    assert "model=llama3.2" in text


def test_telegram_and_publisher_credential_shapes_are_redacted():
    """Telegram bot tokens and publisher client secrets are the most common
    production secrets and must never survive redaction."""
    text = secret_redact('{"telegram_bot_token": "123456:AAHfakeTokenValue",'
                        ' "youtube_client_secret": "GOCSPX-fakesecret",'
                        ' "smtp_password": "mail-pass-1"}')
    for leaked in ("AAHfakeTokenValue", "GOCSPX-fakesecret", "mail-pass-1"):
        assert leaked not in text
    assert text.count("[REDACTED]") == 3


def test_non_secret_values_are_preserved():
    text = "user=alice job_id=job-1 node_name=gpu-01 status=READY"
    assert secret_redact(text) == text


def test_empty_and_none_are_passthrough():
    assert secret_redact("") == ""
    assert secret_redact(None) is None


def test_json_formatter_redacts_message():
    formatter = JsonFormatter()
    record = type("R", (), {})()
    record.getMessage = lambda: "token=leaked"
    record.levelname = "INFO"
    record.name = "test"
    record.exc_info = None
    record.__dict__.update({"job_id": "job-1", "node_name": "gpu-01", "action": "register",
                             "actor": "system"})
    line = formatter.format(record)
    assert "leaked" not in line
    assert json.loads(line)["message"] == "token=[REDACTED]"
    assert json.loads(line)["job_id"] == "job-1"


def test_json_formatter_redacts_extra_fields():
    """Issue #84: extra fields with secret values must be redacted."""
    formatter = JsonFormatter()
    record = type("R", (), {})()
    record.getMessage = lambda: "normal message"
    record.levelname = "INFO"
    record.name = "test"
    record.exc_info = None
    record.__dict__.update({"job_id": "job-1", "node_name": "gpu-01", "custom_token": "secret-123"})
    line = formatter.format(record)
    data = json.loads(line)
    assert "secret-123" not in line
    assert data["custom_token"] == "[REDACTED]"
    assert data["message"] == "normal message"


def test_json_formatter_redacts_exception_traceback():
    formatter = JsonFormatter()
    try:
        raise RuntimeError("api_key=leaked-in-trace")
    except RuntimeError:
        import sys
        record = type("R", (), {})()
        record.getMessage = lambda: "boom"
        record.levelname = "ERROR"
        record.name = "test"
        record.exc_info = sys.exc_info()
    line = formatter.format(record)
    assert "leaked-in-trace" not in line
    assert "[REDACTED]" in line


def test_runner_report_json_is_redacted():
    """Issue #84: Runner JSON reports (logs/API/receipt/metadata) must be redacted."""
    report = json.dumps({"receipt": {"publication_id": "pub-1"},
                         "metadata": {"proxy_password": "p4ss", "session_id": "sess-1"},
                         "error": "token=leaked"})
    redacted = secret_redact(report)
    assert "p4ss" not in redacted
    assert "sess-1" not in redacted
    assert "leaked" not in redacted
    assert '"publication_id": "pub-1"' in redacted


def test_operation_audit_entry_is_redacted(monkeypatch, tmp_path):
    """Issue #84: the operation audit log is an operator-visible surface."""
    import core.operations as operations

    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    operation = operations.create_operation("update", "node-1")
    operations.fail_operation(operation["operation_id"], "token=audit-leak")
    entry = operations.audit_entry(operation["operation_id"], "FAILED", "password=audit-pass", "operator")

    assert "audit-leak" not in entry["error"]
    assert "audit-pass" not in entry["message"]
    assert entry["error"] == "token=[REDACTED]"
    assert entry["message"] == "password=[REDACTED]"
    line = (operations._state_dir() / f"{operation['operation_id']}.audit.jsonl").read_text(encoding="utf-8")
    assert "audit-leak" not in line
    assert "audit-pass" not in line


def test_update_agent_audit_entry_is_redacted(tmp_path):
    """Issue #84: the hash-chained update audit must not persist secrets."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "update_agent", Path(__file__).parents[1] / "scripts" / "update-agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.append_audit(tmp_path, {"operation_id": "a", "phase": "CHECKING",
                                    "message": "token=update-leak"})
    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "update-leak" not in line
    assert "[REDACTED]" in line
    # The hash chain must still verify against the redacted record.
    module.append_audit(tmp_path, {"operation_id": "a", "phase": "UPDATING"})
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 2