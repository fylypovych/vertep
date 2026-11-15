"""i.0.0.4.79 (#479): LLM readiness — the status, config activation and executor agree."""

import json

import pytest
from fastapi.testclient import TestClient

from adapters.llm_clients import LLMBackendError, backend_readiness, get_llm_client
from adapters.providers import provider_matrix
from core.app import app
from core.provider_switch import ProviderSwitchError, reset_provider_registry, switch_provider
from worker import service as worker_service

SECRET = "sk-test-secret-value-should-never-leak"


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch, tmp_path):
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "installation.json").write_text(json.dumps({"completed_at": "2024-01-01T00:00:00Z"}))
    (config / "users.json").write_text("{}")
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    for name in ("VERTEP_LLM_PROVIDER", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reset_provider_registry()
    yield
    reset_provider_registry()


def _executor_accepts() -> bool:
    try:
        client = get_llm_client()
    except LLMBackendError:
        return False
    return getattr(client, "api_key", "present") != ""


@pytest.mark.parametrize("env,key,backend,ready", [
    (None, None, "ollama", True),
    ("", None, "ollama", True),
    ("ollama", None, "ollama", True),
    ("openai", None, "openai", False),
    ("openai", SECRET, "openai", True),
    ("vllm-unknown", None, "vllm-unknown", False),
    ("OpenAI", SECRET, "openai", True),
])
def test_status_matches_the_executor(monkeypatch, env, key, backend, ready):
    if env is not None:
        monkeypatch.setenv("VERTEP_LLM_PROVIDER", env)
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    entry = provider_matrix()["llm"]
    assert (entry["backend"], entry["configured"]) == (backend, ready)
    assert backend_readiness() == (backend, ready)
    # The read-model never claims more than the executor can do.
    assert _executor_accepts() is ready


def test_unknown_backend_is_not_masked_as_ollama(monkeypatch):
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "mystery")
    entry = provider_matrix()["llm"]
    assert entry["backend"] == "mystery" and entry["configured"] is False
    with pytest.raises(LLMBackendError):
        get_llm_client()


def test_status_api_exposes_no_secret(monkeypatch):
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    for path in ("/api/settings/providers", "/api/status"):
        response = TestClient(app).get(path)
        assert SECRET not in response.text, path


class TestConfigActivation:
    def test_core_refuses_switching_to_openai_without_a_key(self):
        with pytest.raises(ProviderSwitchError) as error:
            switch_provider("llm", "openai", actor="tester")
        assert error.value.status_code == 409
        assert provider_matrix()["llm"]["backend"] == "ollama"

    def test_core_switches_to_openai_with_a_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        result = switch_provider("llm", "openai", actor="tester")
        assert result["executor_config"]["VERTEP_LLM_PROVIDER"] == "openai"
        assert SECRET not in json.dumps(result)

    def test_worker_without_a_key_does_not_report_openai_applied(self, monkeypatch):
        """Reproduction of the audit: the delivery used to be accepted by name only."""
        monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
        observed = worker_service.apply_provider_config({"VERTEP_LLM_PROVIDER": "openai"}, "rev-1")
        assert observed["VERTEP_LLM_PROVIDER"] == "ollama"
        assert worker_service._PROVIDER_ACTIVATION_ERROR is True
        import os
        assert os.environ["VERTEP_LLM_PROVIDER"] == "ollama"  # previous config restored

    def test_worker_with_a_key_applies_openai(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", SECRET)
        applied = worker_service.apply_provider_config({"VERTEP_LLM_PROVIDER": "openai"}, "rev-2")
        assert applied == {"VERTEP_LLM_PROVIDER": "openai"}
        assert worker_service._PROVIDER_ACTIVATION_ERROR is False
        assert SECRET not in json.dumps(worker_service.executor_provider_config())

    def test_worker_refuses_an_unknown_backend(self, monkeypatch):
        monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
        observed = worker_service.apply_provider_config({"VERTEP_LLM_PROVIDER": "mystery"}, "rev-3")
        assert observed["VERTEP_LLM_PROVIDER"] == "ollama"
        assert worker_service._PROVIDER_ACTIVATION_ERROR is True

    def test_worker_applies_ollama(self):
        assert worker_service.apply_provider_config({"VERTEP_LLM_PROVIDER": "ollama"}, "rev-4") == {
            "VERTEP_LLM_PROVIDER": "ollama"}
        assert worker_service._PROVIDER_ACTIVATION_ERROR is False
