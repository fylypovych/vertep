"""Тести для modules/audio/ — Audio Module логіка та модуль."""

from modules.audio.audio_logic import (
    normalize_audio_artifact,
    resolve_voice_config,
    validate_voice_task,
)


def test_validate_voice_task_valid():
    task = {"topic": "привіт", "voice": "uk"}
    assert validate_voice_task(task) == []


def test_validate_voice_task_missing_text():
    task = {"voice": "uk"}
    errors = validate_voice_task(task)
    assert any("text/topic" in e for e in errors)


def test_validate_voice_task_missing_voice():
    task = {"topic": "привіт"}
    errors = validate_voice_task(task)
    assert any("voice" in e for e in errors)


def test_resolve_voice_config_defaults():
    config = resolve_voice_config({"topic": "привіт"})
    assert config["provider"] == "default"
    assert config["voice"] == "default"
    assert config["language"] == "uk"
    assert config["speed"] == 150


def test_resolve_voice_config_explicit():
    config = resolve_voice_config({
        "topic": "привіт", "voice": "anna", "language": "uk",
        "model": "tts-1", "speed": 160, "character_id": "char1", "scene_id": "sc1",
    })
    assert config["voice"] == "anna"
    assert config["language"] == "uk"
    assert config["model"] == "tts-1"
    assert config["speed"] == 160
    assert config["character_id"] == "char1"
    assert config["scene_id"] == "sc1"


def test_normalize_audio_artifact():
    data = b"fake audio bytes"
    artifact = normalize_audio_artifact("speech-1.wav", "audio", data, {"voice": "anna"})
    assert artifact["filename"] == "speech-1.wav"
    assert artifact["kind"] == "audio"
    assert artifact["size"] == len(data)
    assert artifact["config"]["voice"] == "anna"


class FakeTTSProvider:
    def __init__(self, audio=b"wav data"):
        self._audio = audio

    def synthesize(self, text, voice="default", speed=150):
        return self._audio


def test_audio_module_execute_voice():
    from modules.audio.audio_module import AudioModule
    module = AudioModule(provider=FakeTTSProvider())
    artifacts = module.execute({
        "task": "voice", "topic": "привіт", "voice": "anna", "scene_id": "sc1",
    })
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "audio"
    assert artifacts[0]["size"] == len(b"wav data")


def test_audio_module_health_check():
    from modules.audio.audio_module import AudioModule
    module = AudioModule()
    health = module.health_check()
    assert health["module"] == "audio"
    assert health["status"] == "READY"


def test_audio_module_resource_query():
    from modules.audio.audio_module import AudioModule
    module = AudioModule()
    res = module.resource_query()
    assert res["gpu_required"] is False
    assert res["min_vram_mb"] == 0


def test_audio_module_cancel_unknown_task():
    from modules.audio.audio_module import AudioModule
    module = AudioModule()
    assert module.cancel("nonexistent") is False
