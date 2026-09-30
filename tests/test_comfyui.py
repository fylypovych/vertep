"""Tests for ``ComfyUIAdapter`` prompt substitution and HTTP contract.

Covers the ``#12`` acceptance item: replacing text-level JSON string
replacement with safe in-structure substitution so that arbitrary topic
values (newlines, backslashes, quotes, Unicode) never corrupt the workflow
JSON sent to ComfyUI.
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.comfyui import ComfyUIAdapter, PromptCancelledError, _substitute_placeholders


# ── _substitute_placeholders unit tests ────────────────────────────────────


class TestSubstitutePlaceholders:
    def test_simple_string(self):
        wf = {"text": "{{TOPIC}}", "clip": ["4", 1]}
        result = _substitute_placeholders(wf, {"TOPIC": "hello world"})
        assert result["text"] == "hello world"

    def test_newline_in_topic(self):
        wf = {"text": "{{TOPIC}}"}
        topic = "line1\nline2"
        result = _substitute_placeholders(wf, {"TOPIC": topic})
        assert result["text"] == "line1\nline2"

    def test_backslash_in_topic(self):
        wf = {"text": "{{TOPIC}}"}
        result = _substitute_placeholders(wf, {"TOPIC": r"C:\Users\test"})
        assert result["text"] == r"C:\Users\test"

    def test_double_quotes_in_topic(self):
        wf = {"text": "{{TOPIC}}"}
        result = _substitute_placeholders(wf, {"TOPIC": 'say "hello"'})
        assert result["text"] == 'say "hello"'

    def test_unicode_in_topic(self):
        wf = {"text": "{{TOPIC}}"}
        result = _substitute_placeholders(wf, {"TOPIC": "Привіт 🌍 日本語"})
        assert result["text"] == "Привіт 🌍 日本語"

    def test_multiple_placeholders(self):
        wf = {"w": "{{WIDTH}}", "h": "{{HEIGHT}}", "cp": "{{CHECKPOINT}}"}
        result = _substitute_placeholders(wf, {"WIDTH": "1024", "HEIGHT": "576",
                                                "CHECKPOINT": "model.safetensors"})
        assert result["w"] == "1024"
        assert result["h"] == "576"
        assert result["cp"] == "model.safetensors"

    def test_nested_dict_and_list(self):
        wf = {"nodes": [{"inputs": {"text": "{{TOPIC}}"}, "class_type": "CLIPTextEncode"}]}
        result = _substitute_placeholders(wf, {"TOPIC": "nested test"})
        assert result["nodes"][0]["inputs"]["text"] == "nested test"

    def test_non_string_values_untouched(self):
        wf = {"seed": 42, "flag": True, "mix": None, "text": "{{TOPIC}}"}
        result = _substitute_placeholders(wf, {"TOPIC": "x"})
        assert result["seed"] == 42
        assert result["flag"] is True
        assert result["mix"] is None

    def test_no_placeholders_returns_same_structure(self):
        wf = {"text": "plain", "num": 7}
        result = _substitute_placeholders(wf, {"TOPIC": "x"})
        assert result == wf

    def test_partial_match_not_replaced(self):
        """{{TOPICS}} must not match {{TOPIC}}."""
        wf = {"text": "{{TOPICS}}"}
        result = _substitute_placeholders(wf, {"TOPIC": "x"})
        assert result["text"] == "{{TOPICS}}"

    def test_newline_and_quote_combined(self):
        """Reproduces the original reported bug: topic with newline + quotes."""
        wf = {"text": "{{TOPIC}}"}
        topic = 'He said:\n"line one"\n"line two"'
        result = _substitute_placeholders(wf, {"TOPIC": topic})
        assert result["text"] == topic

    def test_already_substituted_value_not_double_replaced(self):
        """A topic containing literal {{OTHER}} is not affected."""
        wf = {"text": "{{TOPIC}}"}
        result = _substitute_placeholders(wf, {"TOPIC": "{{OTHER}}"})
        assert result["text"] == "{{OTHER}}"


# ── ComfyUIAdapter integration via mock HTTP ──────────────────────────────


WORKFLOW_JSON = {"1": {"class_type": "CLIPTextEncode",
                        "inputs": {"text": "{{TOPIC}}", "clip": ["4", 1]}}}


def _write_workflow(tmp_path: Path) -> str:
    p = tmp_path / "workflow.json"
    p.write_text(json.dumps(WORKFLOW_JSON), encoding="utf-8")
    return str(p)



class FakeHTTPTransport:
    """Minimal fake capturing POST /prompt and GET /view calls."""

    def __init__(self, image_data: bytes = b"PNG_DATA"):
        self._image_data = image_data
        self.submitted_prompt: dict | None = None

    def post(self, url, json=None, headers=None, timeout=None):
        self.submitted_prompt = json
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"prompt_id": "abc-123"}
        return resp

    def get(self, url, params=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        if "/history/" in url:
            resp.json.return_value = {"abc-123": {
                "outputs": {"1": {"images": [
                    {"filename": "scene-001.png", "subfolder": "", "type": "output"}
                ]}}}}
        elif "/view" in url:
            resp.content = self._image_data
        return resp


@pytest.fixture(autouse=True)
def _demo_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("COMFYUI_URL", "http://fake:8188")
    monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))


class TestGenerateOutputIntegration:
    def test_simple_topic_submitted(self, tmp_path):
        wf = _write_workflow(tmp_path)
        fake = FakeHTTPTransport()
        adapter = ComfyUIAdapter()
        with patch("adapters.comfyui.httpx", MagicMock(post=fake.post, get=fake.get)):
            data, filename, kind = adapter.generate_output(wf, "dogs playing")
        assert kind == "image"
        assert data == b"PNG_DATA"
        assert filename == "scene-001.png"
        assert fake.submitted_prompt["prompt"]["1"]["inputs"]["text"] == "dogs playing"

    def test_newline_topic_not_json_error(self, tmp_path):
        wf = _write_workflow(tmp_path)
        fake = FakeHTTPTransport()
        adapter = ComfyUIAdapter()
        topic = "line1\nline2\nline3"
        with patch("adapters.comfyui.httpx", MagicMock(post=fake.post, get=fake.get)):
            data, _, _ = adapter.generate_output(wf, topic)
        assert data == b"PNG_DATA"
        assert fake.submitted_prompt["prompt"]["1"]["inputs"]["text"] == topic

    def test_backslash_and_quotes_topic(self, tmp_path):
        wf = _write_workflow(tmp_path)
        fake = FakeHTTPTransport()
        adapter = ComfyUIAdapter()
        topic = 'path "C:\\Users\\test\\file" ok'
        with patch("adapters.comfyui.httpx", MagicMock(post=fake.post, get=fake.get)):
            data, _, _ = adapter.generate_output(wf, topic)
        assert data == b"PNG_DATA"
        assert fake.submitted_prompt["prompt"]["1"]["inputs"]["text"] == topic

    def test_unicode_topic(self, tmp_path):
        wf = _write_workflow(tmp_path)
        fake = FakeHTTPTransport()
        adapter = ComfyUIAdapter()
        topic = "Привіт світ 🌍 日本語"
        with patch("adapters.comfyui.httpx", MagicMock(post=fake.post, get=fake.get)):
            data, _, _ = adapter.generate_output(wf, topic)
        assert fake.submitted_prompt["prompt"]["1"]["inputs"]["text"] == topic

    def test_environment_placeholders(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMFYUI_CHECKPOINT", "custom.safetensors")
        monkeypatch.setenv("COMFYUI_SEED", "123")
        wf_path = tmp_path / "wf.json"
        wf_path.write_text(json.dumps({
            "cp": "{{CHECKPOINT}}", "s": "{{SEED}}", "w": "{{WIDTH}}", "h": "{{HEIGHT}}"
        }), encoding="utf-8")
        fake = FakeHTTPTransport()
        adapter = ComfyUIAdapter()
        monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path))
        with patch("adapters.comfyui.httpx", MagicMock(post=fake.post, get=fake.get)):
            adapter.generate_output(str(wf_path), "test")
        assert fake.submitted_prompt["prompt"]["cp"] == "custom.safetensors"
        assert fake.submitted_prompt["prompt"]["s"] == "123"
        assert fake.submitted_prompt["prompt"]["w"] == "768"
        assert fake.submitted_prompt["prompt"]["h"] == "432"

    def test_workflow_path_escape_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKFLOWS_ROOT", str(tmp_path / "safe"))
        adapter = ComfyUIAdapter()
        with pytest.raises(ValueError, match="escapes WORKFLOWS_ROOT"):
            adapter.generate_output(str(tmp_path / "evil.json"), "x")

    def test_workflow_not_found(self, tmp_path):
        adapter = ComfyUIAdapter()
        with pytest.raises(FileNotFoundError):
            adapter.generate_output(str(tmp_path / "missing.json"), "x")

    def test_no_prompt_id_raises(self, tmp_path):
        wf = _write_workflow(tmp_path)
        adapter = ComfyUIAdapter()
        def _no_prompt(url, json=None, headers=None, timeout=None):
            resp = MagicMock()
            resp.json.return_value = {}
            resp.raise_for_status = lambda: None
            return resp
        with patch("adapters.comfyui.httpx", MagicMock(post=_no_prompt)):
            with pytest.raises(RuntimeError, match="no prompt_id"):
                adapter.generate_output(wf, "x")

    def test_no_output_raises(self, tmp_path):
        wf = _write_workflow(tmp_path)
        adapter = ComfyUIAdapter()
        fake = FakeHTTPTransport()
        def _empty_history(url, params=None, headers=None, timeout=None):
            resp = MagicMock()
            resp.json.return_value = {"abc-123": {"outputs": {}}}
            resp.raise_for_status = lambda: None
            return resp
        with patch("adapters.comfyui.httpx", MagicMock(post=fake.post, get=_empty_history)):
            with pytest.raises(RuntimeError, match="without a .* output"):
                adapter.generate_output(wf, "x")


class TestCancelContract:
    """Cancellation must be prompt-scoped and never escalate to a global stop."""

    def test_cancel_without_a_prompt_never_calls_the_backend(self):
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        assert adapter.current_prompt_id is None
        with patch.object(_httpx, "post", side_effect=AssertionError("must not post")) as mock_post, \
             patch.object(_httpx, "delete", side_effect=AssertionError("must not delete")) as mock_delete:
            assert adapter.cancel() is True
        mock_post.assert_not_called()
        mock_delete.assert_not_called()

    def test_cancel_prompt_scoped_delete_first(self):
        """When current_prompt_id is set, DELETE /queue is the only backend call."""
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        adapter.current_prompt_id = "prompt-abc"
        fake_resp = MagicMock(raise_for_status=lambda: None)
        with patch.object(_httpx, "delete", return_value=fake_resp) as mock_del, \
             patch.object(_httpx, "post", return_value=fake_resp) as mock_post:
            assert adapter.cancel() is True
            mock_del.assert_called_once()
            assert "/queue" in mock_del.call_args[0][0]
            assert mock_del.call_args[1]["params"]["prompt_id"] == "prompt-abc"
            mock_post.assert_not_called()
            assert adapter.current_prompt_id is None

    def test_cancel_never_falls_back_to_global_interrupt(self):
        """An executing prompt cannot be removed one by one; it is abandoned.

        Calling the global ``/interrupt`` would abort prompts owned by other jobs,
        other workers and the interactive UI, so it must not happen.
        """
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        adapter.current_prompt_id = "prompt-xyz"
        with patch.object(_httpx, "delete", side_effect=_httpx.HTTPError("already executing")), \
             patch.object(_httpx, "post", side_effect=AssertionError("must not post")) as mock_post:
            assert adapter.cancel() is False
        mock_post.assert_not_called()
        assert adapter.current_prompt_id is None
        # The prompt stays abandoned so its late result is rejected.
        assert "prompt-xyz" in adapter._abandoned_prompts

    def test_late_result_of_an_abandoned_prompt_is_rejected(self, tmp_path):
        """A result produced after the cancellation must never become an artifact."""
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        adapter.current_prompt_id = "prompt-late"
        with patch.object(_httpx, "delete", side_effect=_httpx.HTTPError("already executing")):
            assert adapter.cancel() is False
        history = MagicMock(raise_for_status=lambda: None)
        history.json.return_value = {"prompt-late": {"outputs": {"1": {"images": [
            {"filename": "scene-001.png", "subfolder": "", "type": "output"}]}}}}
        with patch.object(_httpx, "get", return_value=history):
            with pytest.raises(PromptCancelledError):
                adapter.wait_for_result("prompt-late", timeout=5)

    def test_cancelled_prompt_raises_instead_of_returning_media(self, tmp_path):
        """generate_output must fail loudly rather than fetch a cancelled output."""
        wf = _write_workflow(tmp_path)
        fake = FakeHTTPTransport()
        adapter = ComfyUIAdapter()
        real_submit = adapter.submit
        posted = {}

        def _delete(url, params=None, headers=None, timeout=None):
            # ComfyUI cannot remove a prompt that already started executing.
            posted["delete"] = params
            import httpx as _httpx
            raise _httpx.HTTPError("already executing")

        def _submit_then_cancel(workflow):
            response = real_submit(workflow)
            adapter.current_prompt_id = response["prompt_id"]
            adapter.cancel()
            return response

        with patch.object(adapter, "submit", _submit_then_cancel), \
             patch("adapters.comfyui.httpx.post", fake.post), \
             patch("adapters.comfyui.httpx.get", fake.get), \
             patch("adapters.comfyui.httpx.delete", _delete):
            with pytest.raises(PromptCancelledError):
                adapter.generate_output(wf, "x")
        # The prompt-scoped delete was the only backend cancellation call.
        assert posted["delete"]["prompt_id"] == "abc-123"

    def test_cancel_both_fail_returns_false(self):
        """When the prompt-scoped delete fails, cancel reports it was not isolated."""
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        adapter.current_prompt_id = "prompt-fail"
        with patch.object(_httpx, "delete", side_effect=_httpx.HTTPError("err")):
            assert adapter.cancel() is False
        with patch.object(_httpx, "delete", return_value=MagicMock(raise_for_status=lambda: None)):
            assert adapter.cancel() is True

