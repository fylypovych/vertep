"""Issue #78: durable provider backend switch with verify-and-rollback.

Covers the provider half of "Voice preview та provider switch через перевірений
deployment/config flow, із погодженими negative paths": persist → apply →
verify → rollback, plus the HTTP contract the Web UI Settings screen uses.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters.providers import provider_matrix
from core.app import app
from core.provider_switch import (
    ProviderSwitchError,
    apply_provider_overrides,
    load_provider_overrides,
    reset_provider_registry,
    switch_provider,
)

client = TestClient(app)


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point the override store at a temp dir and pin the initial backends."""
    config = tmp_path / "cfg"
    config.mkdir(parents=True, exist_ok=True)
    # Keep the First-Run guard open: the endpoints under test are admin API.
    (config / "installation.json").write_text(
        json.dumps({"completed_at": "2024-01-01T00:00:00Z"}), encoding="utf-8")
    (config / "users.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "native")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    yield config
    reset_provider_registry()


def _overrides_file(config: Path) -> dict:
    path = config / "provider-overrides.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Core switch flow
# ---------------------------------------------------------------------------


def test_switch_persists_applies_and_verifies(isolated_config):
    result = switch_provider("tts", "mock", actor="tester")
    assert result["changed"] is True
    assert result["env"] == "TTS_PROVIDER"
    assert result["matrix"]["tts"]["backend"] == "mock"
    assert provider_matrix()["tts"]["backend"] == "mock"

    stored = _overrides_file(isolated_config)
    assert stored["overrides"]["TTS_PROVIDER"] == "mock"
    assert stored["actor"] == "tester"
    assert stored["slot"] == "tts"
    assert load_provider_overrides() == {"TTS_PROVIDER": "mock"}


def test_switch_same_backend_is_a_noop(isolated_config):
    result = switch_provider("tts", "none", actor="tester")
    assert result["changed"] is False
    assert result["matrix"]["tts"]["backend"] == "none"
    assert _overrides_file(isolated_config) == {}


def test_overrides_are_reapplied_after_restart(isolated_config):
    switch_provider("tts", "mock", actor="tester")
    os.environ.pop("TTS_PROVIDER", None)
    assert provider_matrix()["tts"]["backend"] == "none"
    applied = apply_provider_overrides()
    assert applied == {"TTS_PROVIDER": "mock"}
    reset_provider_registry()
    assert provider_matrix()["tts"]["backend"] == "mock"


def test_unconfigured_backend_rolls_back(isolated_config):
    before = provider_matrix()["llm"]["backend"]
    assert before == "ollama"
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("llm", "openai", actor="tester")
    assert exc.value.status_code == 409
    assert "not configured" in exc.value.message
    # Verification failed, so neither the environment nor the store changed.
    assert provider_matrix()["llm"]["backend"] == "ollama"
    assert load_provider_overrides() == {}
    assert _overrides_file(isolated_config)["overrides"] == {}


def test_unknown_backend_is_rejected_without_side_effects(isolated_config):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("tts", "does-not-exist", actor="tester")
    assert exc.value.status_code == 422
    assert provider_matrix()["tts"]["backend"] == "none"
    assert load_provider_overrides() == {}


def test_missing_backend_is_rejected(isolated_config):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("tts", "", actor="tester")
    assert exc.value.status_code == 422
    assert provider_matrix()["tts"]["backend"] == "none"


def test_unknown_slot_is_rejected(isolated_config):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("nope", "mock", actor="tester")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("slot", ["assembly", "publisher"])
def test_immutable_slots_are_rejected(isolated_config, slot):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider(slot, "anything", actor="tester")
    assert exc.value.status_code == 409
    assert "not switchable" in exc.value.message


def test_apply_provider_overrides_ignores_malformed_env_names(isolated_config):
    config = isolated_config
    config.mkdir(parents=True, exist_ok=True)
    (config / "provider-overrides.json").write_text(json.dumps({
        "overrides": {"TTS_PROVIDER": "mock", "bad name": "x", "lower_case": "y"},
    }), encoding="utf-8")
    applied = apply_provider_overrides()
    assert applied == {"TTS_PROVIDER": "mock"}
    assert "bad name" not in os.environ


# ---------------------------------------------------------------------------
# HTTP contract used by Settings -> Engines
# ---------------------------------------------------------------------------


def test_provider_matrix_endpoint_returns_options(isolated_config):
    response = client.get("/api/settings/providers")
    assert response.status_code == 200
    matrix = response.json()["matrix"]
    assert matrix["tts"]["backend"] == "none"
    assert "mock" in matrix["tts"]["options"]
    assert matrix["tts"]["env"] == "TTS_PROVIDER"


def test_provider_switch_endpoint_success(isolated_config):
    response = client.post("/api/settings/providers/tts", json={"backend": "mock"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slot"] == "tts"
    assert body["backend"] == "mock"
    assert body["changed"] is True
    assert body["matrix"]["tts"]["backend"] == "mock"
    assert _overrides_file(isolated_config)["overrides"]["TTS_PROVIDER"] == "mock"


def test_provider_switch_endpoint_unknown_slot(isolated_config):
    response = client.post("/api/settings/providers/assembly", json={"backend": "ffmpeg"})
    assert response.status_code == 409
    assert "not switchable" in response.json()["detail"]


def test_provider_switch_endpoint_invalid_backend(isolated_config):
    response = client.post("/api/settings/providers/tts", json={"backend": "nope"})
    assert response.status_code == 422
    assert provider_matrix()["tts"]["backend"] == "none"
    assert load_provider_overrides() == {}


def test_provider_switch_endpoint_missing_backend(isolated_config):
    response = client.post("/api/settings/providers/tts", json={})
    assert response.status_code == 422


def test_provider_switch_endpoint_rolls_back_unconfigured(isolated_config):
    response = client.post("/api/settings/providers/llm", json={"backend": "openai"})
    assert response.status_code == 409
    assert provider_matrix()["llm"]["backend"] == "ollama"
    assert load_provider_overrides() == {}


# ---------------------------------------------------------------------------
# HTTP contract used by Settings -> Движок відео (Issue #122 P8)
# ---------------------------------------------------------------------------


def test_the_video_engine_endpoint_renders_both_names_and_the_required_fields(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://user:pass@runtime:8098/internal")
    monkeypatch.delenv("MONEY_PRINTER_TOKEN", raising=False)

    response = client.get("/api/settings/video-engine")

    assert response.status_code == 200
    body = response.json()
    labels = {option["id"]: option["label"] for option in body["options"]}
    assert labels["native"] == "Vertep Native"
    assert labels["money-printer"] == "MoneyPrinterTurbo"
    fields = {field["name"]: field for field in body["fields"]}
    assert fields["endpoint"]["env"] == "MONEY_PRINTER_URL"
    assert fields["endpoint"]["value"] == "http://runtime:8098"
    assert fields["token"]["env"] == "MONEY_PRINTER_TOKEN"
    assert fields["token"]["value"] is None
    assert body["values_exposed"] is False
    assert "pass" not in response.text


def test_a_switch_that_could_carry_a_secret_in_the_endpoint_is_refused(isolated_config):
    response = client.post("/api/settings/providers/video_engine", json={
        "backend": "money-printer", "endpoint": "http://user:pass@runtime:8098",
    })

    assert response.status_code == 422
    assert "credentials" in response.json()["detail"]
    assert load_provider_overrides() == {}


def test_switching_the_engine_is_refused_while_the_system_state_forbids_it(
    isolated_config, monkeypatch
):
    from core.system_state import SystemState, get_system_state, set_system_state

    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.UPDATING, "test rollout")
        response = client.post("/api/settings/providers/video_engine", json={
            "backend": "money-printer", "endpoint": "http://runtime:8098",
        })

        assert response.status_code == 423, response.text
        assert "configuration" in response.text
        assert load_provider_overrides() == {}
    finally:
        set_system_state(previous, "test restore")


def test_the_video_engine_endpoint_reflects_a_state_that_forbids_changes(isolated_config):
    from core.system_state import SystemState, get_system_state, set_system_state

    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.EMERGENCY, "test lock")
        body = client.get("/api/settings/video-engine").json()
        assert body["system_state"] == "EMERGENCY"
        assert body["change_allowed"] is False
    finally:
        set_system_state(previous, "test restore")
