"""Issue #122 P7: effective configuration is persisted, verified and reversible.

P7 does not add a second switching mechanism: selection, persistence, apply and
rollback stay in :mod:`core.provider_switch` (Issue #78). What is proven here is
the engine-specific part:

* selected / effective / config revision always describe one and the same engine;
* the revision is stable for an unchanged configuration and changes when the
  endpoint, the engine or the pinned upstream changes;
* a switch to an external engine is verified **on the actual runtime**, and a
  failed verification keeps the previous effective engine;
* a secret is referenced, never exposed;
* the persisted choice is reproduced after a restart.
"""

from __future__ import annotations

import json

import pytest

from core import engine_config
from core.provider_switch import (ProviderSwitchError, apply_provider_overrides,
                                  load_provider_overrides, switch_provider)


class _Runtime:
    """A stand-in external engine whose readiness the test decides."""

    def __init__(self, ready: bool = True, url: str = "http://runtime:8098",
                 engine_id: str = "money-printer") -> None:
        self._url = url
        self._ready = ready
        self.engine_id = engine_id
        self.name = engine_id
        self.contract_profile = None

    @property
    def provider(self) -> str:
        return self.name

    def configured(self) -> bool:
        return bool(self._url)

    def capabilities(self) -> dict:
        return {"ready": self._ready,
                "health": {"available": self._ready,
                           "reason": None if self._ready else "upstream_unreachable"}}


class _Native:
    engine_id = "native"
    provider = "native"
    name = "native"
    contract_profile = None

    def configured(self) -> bool:
        return True

    def capabilities(self) -> dict:
        return {"ready": True}


@pytest.fixture
def registry(monkeypatch):
    """A controllable provider registry for the video-engine slot.

    The fake matrix reports the *selected* backend from the environment, while the
    effective engine is whatever the test put into the registry — the same split a
    real process has between a persisted choice and a resolved factory.
    """
    import os

    import adapters.providers as providers_module

    current = {"engine": _Native()}

    def _matrix() -> dict:
        selected = os.getenv("VERTEP_VIDEO_ENGINE", "") or "native"
        return {
            "video_engine": {
                "backend": selected,
                "options": ["native", "money-printer"],
                "env": "VERTEP_VIDEO_ENGINE",
                "configured": True,
            },
        }

    monkeypatch.setattr(providers_module, "providers", type("_P", (), {
        "video_engine": staticmethod(lambda: current["engine"]),
    }))
    monkeypatch.setattr(providers_module, "provider_matrix", _matrix)
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "")
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    # Each test starts from a clean, persisted selection: the configuration root is
    # shared by the whole suite, so a leftover override would hide a rollback.
    from core.provider_switch import _write_overrides

    _write_overrides({}, actor="test-setup", slot="video_engine")
    return current


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def test_selected_effective_and_revision_describe_one_engine(registry):
    registry["engine"] = _Runtime(ready=True)
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
        config = engine_config.effective_engine_config()
        revision = engine_config.engine_config_revision()

    assert config["selected"] == "money-printer"
    assert config["effective"] == "money-printer"
    assert config["agree"] is True
    assert config["values_exposed"] is False
    assert config["config_revision"] == revision

    # Without a probe the read model never claims readiness it has not checked.
    assert config["ready"] is None
    assert config["reason"] == "not_probed"
    assert engine_config.effective_engine_config(probe=True)["ready"] is True


def test_a_secret_is_referenced_and_never_exposed(registry):
    registry["engine"] = _Runtime(ready=True)
    monkey = pytest.MonkeyPatch()
    monkey.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkey.setenv("MONEY_PRINTER_TOKEN", "super-secret-token")
    try:
        config = engine_config.effective_engine_config()
    finally:
        monkey.undo()

    assert config["secret"] == {"env": "MONEY_PRINTER_TOKEN", "configured": True,
                                "source": "env"}
    assert "super-secret-token" not in json.dumps(config)


def test_the_revision_is_stable_and_changes_with_the_configuration(registry, monkeypatch):
    registry["engine"] = _Runtime(ready=True)
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "first")

    first = engine_config.engine_config_revision()
    assert first == engine_config.engine_config_revision()

    # A secret value is not part of the revision: rotating it must not invalidate a
    # running attempt.
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "rotated")
    assert engine_config.engine_config_revision() == first

    # Repointing the endpoint does change it.
    registry["engine"] = _Runtime(ready=True, url="http://other-runtime:8098")
    assert engine_config.engine_config_revision() != first


def test_the_endpoint_identity_carries_no_credentials(registry, monkeypatch):
    registry["engine"] = _Runtime(
        ready=True, url="http://user:password@runtime:8098/private?token=secret#frag")

    identity = engine_config.endpoint_identity(registry["engine"])

    assert identity == "http://runtime:8098"
    assert "password" not in identity and "secret" not in identity


def test_an_unreachable_runtime_is_reported_as_not_ready(registry, monkeypatch):
    registry["engine"] = _Runtime(ready=False)
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")

    report = engine_config.verify_effective_engine(probe=True)

    assert report["ready"] is False
    assert report["reason"] == "upstream_unreachable"
    assert report["secret"]["env"] == "MONEY_PRINTER_TOKEN"


# ---------------------------------------------------------------------------
# Persist → apply → verify → rollback on the real executor
# ---------------------------------------------------------------------------


def test_a_switch_is_verified_on_the_runtime_and_reported_with_its_configuration(
    registry, monkeypatch
):
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "token")
    registry["engine"] = _Runtime(ready=True)

    result = switch_provider("video_engine", "money-printer", actor="test")

    assert result["changed"] is True
    assert result["effective_engine"]["effective"] == "money-printer"
    assert result["effective_engine"]["ready"] is True
    assert load_provider_overrides()["VERTEP_VIDEO_ENGINE"] == "money-printer"


def test_a_failed_verify_keeps_the_previous_effective_engine(registry, monkeypatch):
    """A runtime that cannot prove readiness must not become effective."""
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    registry["engine"] = _Runtime(ready=False)
    before = engine_config.effective_engine_config()

    with pytest.raises(ProviderSwitchError) as refusal:
        switch_provider("video_engine", "money-printer", actor="test")

    assert refusal.value.status_code == 409
    assert "not ready" in refusal.value.message
    after = engine_config.effective_engine_config()
    assert after["effective"] == before["effective"]
    assert after["config_revision"] == before["config_revision"]
    assert "VERTEP_VIDEO_ENGINE" not in load_provider_overrides(), \
        "a failed apply must not persist the new selection"


def test_an_unknown_backend_is_refused(registry):
    with pytest.raises(ProviderSwitchError) as refusal:
        switch_provider("video_engine", "not-an-engine", actor="test")

    assert refusal.value.status_code == 422


def test_the_persisted_choice_is_reproduced_after_a_restart(registry, monkeypatch):
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "")
    registry["engine"] = _Native()

    class _Factory:
        """A factory that only honours the environment, like a fresh process."""

        def video_engine(self):
            selected = __import__("os").getenv("VERTEP_VIDEO_ENGINE", "")
            return _Runtime(ready=True) if selected == "money-printer" else _Native()

    import adapters.providers as providers_module

    monkeypatch.setattr(providers_module, "providers", _Factory())
    switch_provider("video_engine", "money-printer", actor="test")

    # Simulate the restart: a fresh process reads the persisted overrides again.
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "")
    monkeypatch.setattr(providers_module, "_providers", None, raising=False)
    apply_provider_overrides()

    assert providers_module.providers.video_engine().engine_id == "money-printer"
    assert load_provider_overrides()["VERTEP_VIDEO_ENGINE"] == "money-printer"