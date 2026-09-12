import base64
import hashlib

from worker import role_executor


class Response:
    headers = {"content-type": "application/json"}
    content = b""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def test_text_executor_returns_utf8_artifact(monkeypatch):
    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs: Response({"response": "Вітаю"}))
    [artifact] = role_executor.execute_role_task("text", {"task": "text", "topic": "hello"})
    assert artifact["filename"] == "response.txt"
    assert base64.b64decode(artifact["data_base64"]).decode() == "Вітаю"


def test_publisher_executor_returns_receipt(monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    [artifact] = role_executor.execute_role_task("publisher", {"task": "publish", "channel": "youtube",
        "topic": "x", "job_id": "j", "video_path": "", "metadata": {}})
    assert artifact["filename"] == "publication.json"
    assert artifact["kind"] == "publication_receipt"
    import json as _json
    receipt = _json.loads(base64.b64decode(artifact["data_base64"]).decode("utf-8"))
    assert receipt["channel"] == "youtube"
    assert receipt["status"] == "PUBLISHED"


def test_publisher_executor_rejects_unconfigured_channel(monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    [artifact] = role_executor.execute_role_task("publisher", {"task": "publish", "channel": "youtube",
        "topic": "x", "job_id": "j", "video_path": "", "metadata": {}})
    receipt = base64.b64decode(artifact["data_base64"]).decode("utf-8")
    assert "NOT_CONFIGURED" in receipt


def test_voice_worker_emits_verifiable_audio_contract(monkeypatch):
    audio = b"RIFF\x04\x00\x00\x00WAVE"
    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs:
                        Response({"audio_base64": base64.b64encode(audio).decode(),
                                  "mime_type": "audio/wav", "engine": "espeak-ng"}))
    [artifact] = role_executor.execute_role_task("voice", {
        "task": "voice", "topic": "привіт", "provider": "mock", "voice": "uk",
        "language": "uk", "scene_id": "scene-001", "character_id": "voicechar",
        "speed": 160})
    assert base64.b64decode(artifact["data_base64"]) == audio
    contract = artifact["contract"]
    assert contract["format"] == "audio_contract/v1"
    assert contract["provider"] == "mock"
    assert contract["voice"] == "uk"
    assert contract["language"] == "uk"
    assert contract["engine"] == "espeak-ng"
    assert contract["scene_id"] == "scene-001"
    assert contract["character_id"] == "voicechar"
    assert contract["sha256"] == hashlib.sha256(audio).hexdigest()
    assert contract["size"] == len(audio)


def test_voice_worker_rejects_disabled_provider_without_calling_runtime(monkeypatch):
    called = {"post": False}

    def _post(*args, **kwargs):
        called["post"] = True
        return Response({})

    monkeypatch.setattr(role_executor.httpx, "post", _post)
    try:
        role_executor.execute_role_task("voice", {"task": "voice", "topic": "x",
                                                   "provider": "disabled"})
    except RuntimeError as error:
        assert "disabled" in str(error)
    else:
        raise AssertionError("disabled provider was accepted")
    assert called["post"] is False


def test_script_executor_returns_script_artifact():
    import json
    [artifact] = role_executor.execute_role_task("text", {"task": "script", "topic": "тест",
                       "system_prompt": "", "character": {}})
    assert artifact["filename"] == "script.json"
    assert artifact["kind"] == "script"
    data = json.loads(base64.b64decode(artifact["data_base64"]).decode("utf-8"))
    assert "title" in data and "scenes" in data


def test_role_cannot_execute_another_roles_task():
    try:
        role_executor.execute_role_task("text", {"task": "publish", "job_id": "j", "topic": "x"})
    except PermissionError as error:
        assert "not authorized" in str(error)
    else:
        raise AssertionError("cross-role task execution was accepted")
