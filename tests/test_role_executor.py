import base64

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


def test_voice_and_publisher_require_valid_runtime_receipts(monkeypatch):
    audio = b"RIFF\x04\x00\x00\x00WAVE"
    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs:
                        Response({"audio_base64": base64.b64encode(audio).decode()}))
    [artifact] = role_executor.execute_role_task("voice", {"task": "voice", "topic": "hello"})
    assert base64.b64decode(artifact["data_base64"]) == audio

    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs: Response({}))
    try:
        role_executor.execute_role_task("publisher", {"task": "publish", "topic": "x", "job_id": "j"})
    except RuntimeError as error:
        assert "publication receipt" in str(error)
    else:
        raise AssertionError("invalid publisher receipt was accepted")


def test_role_cannot_execute_another_roles_task():
    try:
        role_executor.execute_role_task("text", {"task": "publish", "job_id": "j", "topic": "x"})
    except PermissionError as error:
        assert "not authorized" in str(error)
    else:
        raise AssertionError("cross-role task execution was accepted")
