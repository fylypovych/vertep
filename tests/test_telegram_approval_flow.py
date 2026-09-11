"""Tests for Telegram approval flow: script, storyboard, and video approval."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from core.models import JobStatus


def _make_job(status_str="SCRIPT_PENDING_APPROVAL", source="telegram:42",
              job_id="2026-000001"):
    status = JobStatus(status_str)
    return SimpleNamespace(
        job_id=job_id, topic="Test topic", character_id="did_samogon",
        status=status, source=source,
        script={"title": "Тест", "scenes": [
            {"prompt": "Кадр", "video_prompt": "Рух", "voiceover": "Текст", "duration": 5}
        ]}, output_path=None, approval_status="pending", approved=False, version=1,
        storyboard_revision_chat_id=None, storyboard_revision_version=None,
        script_revision_chat_id=None, script_revision_pending=False,
        video_revision_chat_id=None, video_revision_pending=False,
        model_dump=lambda **kw: {"job_id": job_id},
    )


class TestScriptApprovalKeyboard:
    def test_structure(self):
        from core.storyboard_telegram import script_keyboard
        kb = script_keyboard("2026-000001")
        rows = kb["inline_keyboard"]
        assert len(rows) == 2
        assert any("sc_ok" in b["callback_data"] for b in rows[0])
        assert any("sc_regen" in b["callback_data"] for b in rows[0])
        assert any("sc_edit" in b["callback_data"] for b in rows[1])
        assert any("sc_reject" in b["callback_data"] for b in rows[1])


class TestVideoApprovalKeyboard:
    def test_structure(self):
        from core.storyboard_telegram import video_approval_keyboard
        kb = video_approval_keyboard("2026-000001")
        rows = kb["inline_keyboard"]
        assert len(rows) == 2
        assert any("vid_ok" in b["callback_data"] for b in rows[0])
        assert any("vid_regen" in b["callback_data"] for b in rows[0])
        assert any("vid_edit" in b["callback_data"] for b in rows[1])
        assert any("vid_reject" in b["callback_data"] for b in rows[1])


class TestRenderScript:
    def test_produces_chunks(self):
        from core.storyboard_telegram import render_script
        script = {"title": "Заголовок", "description": "Опис",
                  "hashtags": ["tag1"], "scenes": [
                      {"prompt": "Кадр", "video_prompt": "Рух",
                       "voiceover": "Текст", "duration": 5}]}
        chunks = render_script(script, "2026-000001")
        assert len(chunks) >= 1
        assert "2026-000001" in chunks[0]

    def test_empty_script(self):
        from core.storyboard_telegram import render_script
        chunks = render_script({}, "2026-000001")
        assert len(chunks) >= 1


class TestScriptCallbackHandlers:
    def test_sc_ok_approves(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("SCRIPT_PENDING_APPROVAL")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "update", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "transition", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "event", lambda j, e: j)
        cb = {"id": "cb-1", "data": "sc_ok:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_script_callback(cb, "42", "sc_ok", "2026-000001")
        assert adapter.answer_callback.called

    def test_sc_reject_cancels(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("SCRIPT_PENDING_APPROVAL")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "update", lambda j, s, e: setattr(j, "status", s) or j)
        cb = {"id": "cb-2", "data": "sc_reject:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_script_callback(cb, "42", "sc_reject", "2026-000001")
        assert job.status.value == "CANCELLED"

    def test_sc_edit_sets_flag(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("SCRIPT_PENDING_APPROVAL")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "event", lambda j, e: None)
        cb = {"id": "cb-3", "data": "sc_edit:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_script_callback(cb, "42", "sc_edit", "2026-000001")
        assert job.script_revision_chat_id == "42"
        assert job.script_revision_pending is True

    def test_sc_not_found(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module.store, "jobs", {})
        cb = {"id": "cb-4", "data": "sc_ok:nope", "message": {"chat": {"id": "42"}}}
        module._handle_script_callback(cb, "42", "sc_ok", "nope")
        assert adapter.answer_callback.called
        assert "не знайдено" in adapter.answer_callback.call_args[0][1]


class TestVideoCallbackHandlers:
    def test_vid_ok_approves(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("VIDEO_PENDING_APPROVAL")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "update", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "transition", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "event", lambda j, e: j)
        cb = {"id": "cb-1", "data": "vid_ok:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_video_callback(cb, "42", "vid_ok", "2026-000001")
        assert job.status.value == "READY"

    def test_vid_reject_cancels(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("VIDEO_PENDING_APPROVAL")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "update", lambda j, s, e: setattr(j, "status", s) or j)
        cb = {"id": "cb-2", "data": "vid_reject:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_video_callback(cb, "42", "vid_reject", "2026-000001")
        assert job.status.value == "CANCELLED"

    def test_vid_edit_sets_flag(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("VIDEO_PENDING_APPROVAL")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "event", lambda j, e: None)
        cb = {"id": "cb-3", "data": "vid_edit:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_video_callback(cb, "42", "vid_edit", "2026-000001")
        assert job.video_revision_chat_id == "42"
        assert job.video_revision_pending is True

    def test_vid_not_found(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        monkeypatch.setattr(module.store, "jobs", {})
        cb = {"id": "cb-4", "data": "vid_ok:nope", "message": {"chat": {"id": "42"}}}
        module._handle_video_callback(cb, "42", "vid_ok", "nope")
        assert adapter.answer_callback.called
        assert "не знайдено" in adapter.answer_callback.call_args[0][1]


class TestRevisionTextHandlers:
    def test_script_revision_clears_flag(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("SCRIPT_PENDING_APPROVAL")
        job.script_revision_chat_id = "42"
        job.script_revision_pending = True
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "transition", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "event", lambda j, e: None)
        msg = {"text": "Змінити заголовок", "chat": {"id": "42"}, "message_id": 1}
        module._handle_telegram_message("42", "1", "Змінити заголовок", msg)
        assert job.script_revision_chat_id is None
        assert job.script_revision_pending is False

    def test_video_revision_clears_flag(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("VIDEO_PENDING_APPROVAL")
        job.video_revision_chat_id = "42"
        job.video_revision_pending = True
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "transition", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "event", lambda j, e: None)
        msg = {"text": "Змінити тривалість", "chat": {"id": "42"}, "message_id": 1}
        module._handle_telegram_message("42", "1", "Змінити тривалість", msg)
        assert job.video_revision_chat_id is None
        assert job.video_revision_pending is False


class TestPipelineVideoApproval:
    def test_approve_video_transitions_to_ready(self):
        from unittest.mock import patch
        from core.pipeline import approve_video
        job = _make_job("VIDEO_PENDING_APPROVAL")
        store = SimpleNamespace(
            transition=lambda j, s, e: setattr(j, "status", s) or j,
            event=lambda j, e: j,
        )
        with patch("core.pipeline._progress"):
            result = approve_video(store, job, "telegram:42")
        assert result.status.value == "READY"

    def test_approve_video_rejects_wrong_status(self):
        from core.pipeline import approve_video
        job = _make_job("SCRIPT_PENDING_APPROVAL")
        store = SimpleNamespace()
        with pytest.raises(ValueError, match="Cannot approve video"):
            approve_video(store, job, "telegram:42")


class TestCallbackIdempotency:
    def test_double_sc_ok_no_crash(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("SCRIPT_APPROVED")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "update", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "transition", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "event", lambda j, e: j)
        cb = {"id": "cb-1", "data": "sc_ok:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_script_callback(cb, "42", "sc_ok", "2026-000001")
        assert adapter.answer_callback.called

    def test_double_vid_ok_no_crash(self, monkeypatch):
        module = importlib.import_module("core.app")
        adapter = Mock()
        monkeypatch.setattr(module, "TelegramAdapter", lambda: adapter)
        job = _make_job("READY")
        monkeypatch.setattr(module.store, "jobs", {"2026-000001": job})
        monkeypatch.setattr(module.store, "update", lambda j, s, e: setattr(j, "status", s) or j)
        monkeypatch.setattr(module.store, "transition", lambda j, s, e: setattr(j, "status", s) or j)
        cb = {"id": "cb-1", "data": "vid_ok:2026-000001", "message": {"chat": {"id": "42"}}}
        module._handle_video_callback(cb, "42", "vid_ok", "2026-000001")
        assert adapter.answer_callback.called
