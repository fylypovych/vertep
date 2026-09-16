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

from adapters.comfyui import ComfyUIAdapter, _substitute_placeholders


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
    def test_cancel_returns_true_via_global_interrupt(self):
        """Without a stored prompt_id, cancel falls back to POST /interrupt."""
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        assert adapter.current_prompt_id is None
        fake_resp = MagicMock(raise_for_status=lambda: None)
        with patch.object(_httpx, "post", return_value=fake_resp) as mock_post:
            assert adapter.cancel() is True
        mock_post.assert_called_once()
        assert "/interrupt" in mock_post.call_args[0][0]

    def test_cancel_catches_http_error(self):
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        with patch.object(_httpx, "post", side_effect=_httpx.HTTPError("fail")):
            assert adapter.cancel() is False

    def test_cancel_prompt_scoped_delete_first(self):
        """When current_prompt_id is set, DELETE /queue is tried before /interrupt."""
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
            # Global interrupt should NOT be called when delete succeeds
            mock_post.assert_not_called()
            assert adapter.current_prompt_id is None

    def test_cancel_prompt_scoped_falls_back_to_interrupt(self):
        """When DELETE /queue fails, cancel falls back to global /interrupt."""
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        adapter.current_prompt_id = "prompt-xyz"
        fake_resp = MagicMock(raise_for_status=lambda: None)
        with patch.object(_httpx, "delete", side_effect=_httpx.HTTPError("gone")), \
             patch.object(_httpx, "post", return_value=fake_resp) as mock_post:
            assert adapter.cancel() is True
            mock_post.assert_called_once()
            assert "/interrupt" in mock_post.call_args[0][0]
            assert adapter.current_prompt_id is None

    def test_cancel_both_fail_returns_false(self):
        """When both DELETE and /interrupt fail, cancel returns False."""
        import httpx as _httpx
        adapter = ComfyUIAdapter()
        adapter.current_prompt_id = "prompt-fail"
        with patch.object(_httpx, "delete", side_effect=_httpx.HTTPError("err")), \
             patch.object(_httpx, "post", side_effect=_httpx.HTTPError("err")):
            assert adapter.cancel() is False

