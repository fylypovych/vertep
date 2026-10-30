"""Тести для worker/roles/ — role launchers з feature flag перемиканням."""

import base64
import os

from worker.roles.text_node import execute as text_execute, health_check as text_health
from worker.roles.gpu_node import execute as gpu_execute, health_check as gpu_health
from worker.roles.voice_node import execute as voice_execute, health_check as voice_health

import worker.role_executor as role_executor


class _Response:
    headers = {"content-type": "application/json"}
    content = b""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def _set_flag(flag, value):
    os.environ[flag] = value


def _mock_httpx_post(monkeypatch, response_value):
    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs: _Response(response_value))


def test_text_role_original_path_when_flag_off(monkeypatch):
    _set_flag("VERTEP_TEXT_MODULE_ENABLED", "false")
    _mock_httpx_post(monkeypatch, {"response": "Вітаю"})
    artifacts = text_execute({"task": "text", "topic": "hello"})
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "text"
    assert base64.b64decode(artifacts[0]["data_base64"]).decode() == "Вітаю"


def test_text_role_module_path_when_flag_on(monkeypatch):
    _set_flag("VERTEP_TEXT_MODULE_ENABLED", "true")
    _mock_httpx_post(monkeypatch, {"response": "Вітаю"})
    artifacts = text_execute({"task_id": "text-module-role", "task": "text", "topic": "hello"})
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "text"


def test_text_role_health_check():
    health = text_health()
    assert "role" in health or "module" in health


def test_gpu_role_original_path_when_flag_off(monkeypatch):
    _set_flag("VERTEP_IMAGE_MODULE_ENABLED", "false")
    _mock_httpx_post(monkeypatch, {"data": base64.b64encode(b"img").decode()})
    artifacts = gpu_execute({"task": "image", "topic": "кіт", "workflow": "wf.json"})
    assert len(artifacts) == 1


def test_gpu_role_module_path_when_flag_on():
    _set_flag("VERTEP_IMAGE_MODULE_ENABLED", "true")

    class FakeProvider:
        def generate_output(self, workflow, topic, task_type="image"):
            return b"img", "out.png", "image"

    from modules.image.image_module import ImageModule
    import worker.roles.gpu_node as gpu_node_mod
    gpu_node_mod._image_module = ImageModule(provider=FakeProvider())
    artifacts = gpu_execute({"task": "image", "topic": "кіт", "workflow": "wf.json"})
    assert len(artifacts) == 1


def test_gpu_role_health_check():
    health = gpu_health()
    assert "role" in health or "module" in health


def test_voice_role_original_path_when_flag_off(monkeypatch):
    _set_flag("VERTEP_AUDIO_MODULE_ENABLED", "false")
    _mock_httpx_post(monkeypatch, {"audio_base64": base64.b64encode(b"wav").decode(), "mime_type": "audio/wav"})
    artifacts = voice_execute({"task": "voice", "topic": "привіт", "voice": "anna"})
    assert len(artifacts) == 1


def test_voice_role_module_path_when_flag_on():
    _set_flag("VERTEP_AUDIO_MODULE_ENABLED", "true")

    class FakeTTSProvider:
        def synthesize(self, text, voice="default", speed=150):
            return b"wav data"

    from modules.audio.audio_module import AudioModule
    import worker.roles.voice_node as voice_node_mod
    voice_node_mod._audio_module = AudioModule(provider=FakeTTSProvider())
    artifacts = voice_execute({"task": "voice", "topic": "привіт", "voice": "anna"})
    assert len(artifacts) == 1


def test_voice_role_health_check():
    health = voice_health()
    assert "role" in health or "module" in health
