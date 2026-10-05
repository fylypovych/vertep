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
import os

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


class _EnvRuntime(_Runtime):
    """An external engine that resolves its address the way the real factory does.

    A process rebuilds its engine from the persisted environment, so an applied endpoint
    becomes visible without anything rewriting the engine object. Reading the environment
    on access is what makes persist → apply → verify → restart observable in a test
    instead of being asserted by hand.
    """

    def __init__(self, ready: bool = True, engine_id: str = "money-printer") -> None:
        super().__init__(ready=ready, url="", engine_id=engine_id)

    @property
    def _url(self) -> str:
        return os.getenv("MONEY_PRINTER_URL", "")

    @_url.setter
    def _url(self, value: str) -> None:
        return None


@pytest.fixture
def registry(monkeypatch):
    """A controllable provider registry for the video-engine slot.

    The fake matrix reports the *selected* backend from the environment, while the
    effective engine is whatever the test put into the registry — the same split a
    real process has between a persisted choice and a resolved factory.
    """
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


# ---------------------------------------------------------------------------
# P8: the read model Settings renders
# ---------------------------------------------------------------------------


def test_the_read_model_names_both_engines_and_lists_the_required_fields(registry, monkeypatch):
    registry["engine"] = _Runtime(ready=True)
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://user:pass@runtime:8098/internal?token=s3cret")
    monkeypatch.delenv("MONEY_PRINTER_TOKEN", raising=False)

    config = engine_config.effective_engine_config()

    labels = {option["id"]: option["label"] for option in config["options"]}
    assert labels["native"] == "Vertep Native"
    assert labels["money-printer"] == "MoneyPrinterTurbo"

    fields = {field["name"]: field for field in config["fields"]}
    assert set(fields) == {"endpoint", "token"}
    assert fields["endpoint"]["env"] == "MONEY_PRINTER_URL"
    assert fields["endpoint"]["configured"] is True
    assert fields["endpoint"]["value"] == "http://runtime:8098", \
        "the reported endpoint must be an identity, not the raw value"
    assert fields["token"]["env"] == "MONEY_PRINTER_TOKEN"
    assert fields["token"]["configured"] is False
    assert fields["token"]["value"] is None, "a secret value must never be reported"
    serialized = json.dumps(config)
    assert "pass" not in serialized and "s3cret" not in serialized


def test_the_read_model_reports_the_current_system_state_and_its_change_permission(
    registry, monkeypatch
):
    from core.system_state import SystemState, get_system_state, set_system_state

    registry["engine"] = _Native()
    previous = get_system_state()["state"]
    try:
        config = engine_config.effective_engine_config()
        assert config["system_state"] == previous
        assert config["change_allowed"] is True

        set_system_state(SystemState.UPDATING, "test rollout")
        blocked = engine_config.effective_engine_config()
        assert blocked["system_state"] == "UPDATING"
        assert blocked["change_allowed"] is False
    finally:
        set_system_state(previous, "test restore")


def test_the_read_model_exposes_no_secret_value(registry, monkeypatch):
    registry["engine"] = _Runtime(ready=True)
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "super-secret-token")

    config = engine_config.effective_engine_config()

    assert config["secret"]["env"] == "MONEY_PRINTER_TOKEN"
    assert "super-secret-token" not in json.dumps(config)


def test_the_read_model_lists_the_inputs_of_every_engine_not_only_the_effective_one(
    registry, monkeypatch
):
    """Settings must be able to fill in an engine that is not effective yet."""
    registry["engine"] = _Native()
    monkeypatch.setenv("MONEY_PRINTER_URL", "")
    monkeypatch.delenv("MONEY_PRINTER_TOKEN", raising=False)

    config = engine_config.effective_engine_config()

    assert config["effective"] == "native"
    assert config["fields"] == [], "Vertep Native needs no external runtime"
    external = {field["name"]: field for field in config["required_fields"]["money-printer"]}
    assert external["endpoint"]["configured"] is False
    assert external["token"]["configured"] is False
    assert set(config["required_fields"]) == {option["id"] for option in config["options"]}


# ---------------------------------------------------------------------------
# P8: the endpoint can be given through the same apply → verify → rollback flow
# ---------------------------------------------------------------------------


def _written_overrides() -> dict[str, str]:
    """The overrides as they are actually persisted.

    :func:`core.provider_switch.load_provider_overrides` applies them to ``os.environ``
    on load, so an env-only value is indistinguishable from a persisted one afterwards.
    P8 has to prove what survives a restart, which is exactly what the file holds.
    """
    import json as _json
    from core.provider_switch import _overrides_path

    raw = _json.loads(_overrides_path().read_text(encoding="utf-8"))
    return dict(raw.get("overrides") or raw)


def test_an_endpoint_applied_together_with_the_engine_is_persisted_and_reported(
    registry, monkeypatch
):
    monkeypatch.setenv("MONEY_PRINTER_URL", "")
    monkeypatch.delenv("MONEY_PRINTER_TOKEN", raising=False)
    registry["engine"] = _Runtime(ready=True)

    result = switch_provider("video_engine", "money-printer", actor="test",
                             endpoint="http://runtime:8098")

    assert result["changed"] is True
    assert result["effective_engine"]["effective"] == "money-printer"
    overrides = _written_overrides()
    assert overrides["VERTEP_VIDEO_ENGINE"] == "money-printer"
    assert overrides["MONEY_PRINTER_URL"] == "http://runtime:8098"


def test_a_refused_apply_rolls_the_endpoint_back_as_well(registry, monkeypatch):
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    registry["engine"] = _Runtime(ready=False)
    before = engine_config.effective_engine_config()

    with pytest.raises(ProviderSwitchError):
        switch_provider("video_engine", "money-printer", actor="test",
                        endpoint="http://other:9000")

    assert "MONEY_PRINTER_URL" not in _written_overrides()
    after = engine_config.effective_engine_config()
    assert after["effective"] == before["effective"]
    assert after["config_revision"] == before["config_revision"]


@pytest.mark.parametrize("endpoint", [
    "http://user:password@runtime:8098",   # credentials in the URL
    "http://runtime:8098/internal",       # a path is not an origin
    "http://runtime:8098?token=secret",   # a query is not an origin
    "ftp://runtime",                      # not an HTTP runtime
    "runtime:8098",                        # no scheme
    "",                                   # empty
])
def test_an_endpoint_that_could_carry_a_secret_or_is_not_an_origin_is_refused(
    registry, monkeypatch, endpoint
):
    monkeypatch.setenv("MONEY_PRINTER_URL", "")
    registry["engine"] = _Runtime(ready=True)

    with pytest.raises(ProviderSwitchError) as refusal:
        switch_provider("video_engine", "money-printer", actor="test", endpoint=endpoint)

    assert refusal.value.status_code == 422
    assert "MONEY_PRINTER_URL" not in load_provider_overrides()
    assert "VERTEP_VIDEO_ENGINE" not in load_provider_overrides()


def test_native_has_no_endpoint_to_give(registry, monkeypatch):
    registry["engine"] = _Native()

    with pytest.raises(ProviderSwitchError) as refusal:
        switch_provider("video_engine", "native", actor="test", endpoint="http://runtime:8098")

    assert refusal.value.status_code == 422


def test_the_runtime_address_of_the_effective_engine_can_be_repointed(registry, monkeypatch):
    """P8: адресу runtime змінюють без перемикання движка — і це теж apply.

    Зовнішній движок вказується на свій runtime адресою, тому її зміна є зміною ефективної
    конфігурації навіть тоді, коли сам движок лишається тим самим. Раніше такий запит не
    проходив через жоден маршрут: Settings не надсилав його, а apply без зміни движка
    повертався як ``changed: false``.
    """
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    registry["engine"] = _EnvRuntime(ready=True)
    switch_provider("video_engine", "money-printer", actor="test",
                    endpoint="http://runtime:8098")
    before = engine_config.effective_engine_config()
    assert before["effective"] == "money-printer"
    assert before["fields"][0]["value"] == "http://runtime:8098"

    result = switch_provider("video_engine", "money-printer", actor="test",
                             endpoint="http://runtime:9090")

    assert result["changed"] is True
    after = result["effective_engine"]
    assert after["effective"] == "money-printer", "движок лишається тим самим"
    assert after["endpoint"] == "http://runtime:9090"
    assert after["fields"][0]["value"] == "http://runtime:9090"
    assert after["config_revision"] != before["config_revision"], \
        "інша адреса — це інша ревізія конфігурації"
    assert _written_overrides()["MONEY_PRINTER_URL"] == "http://runtime:9090"


def test_a_refused_repointing_keeps_the_previous_runtime_address(registry, monkeypatch):
    """Нездатний перевірити runtime не лишає Job на іншій адресі."""
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    registry["engine"] = _EnvRuntime(ready=True)
    switch_provider("video_engine", "money-printer", actor="test",
                    endpoint="http://runtime:8098")
    registry["engine"] = _EnvRuntime(ready=False)

    with pytest.raises(ProviderSwitchError) as refusal:
        switch_provider("video_engine", "money-printer", actor="test",
                        endpoint="http://other:9000")

    assert refusal.value.status_code == 409
    assert _written_overrides()["MONEY_PRINTER_URL"] == "http://runtime:8098"
    assert engine_config.effective_engine_config()["endpoint"] == "http://runtime:8098"


def test_a_repeated_switch_without_an_endpoint_is_a_no_op(registry, monkeypatch):
    """Нічого не змінювати — нічого й не переписувати."""
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    registry["engine"] = _EnvRuntime(ready=True)
    switch_provider("video_engine", "money-printer", actor="test",
                    endpoint="http://runtime:8098")

    result = switch_provider("video_engine", "money-printer", actor="test")

    assert result["changed"] is False
    assert _written_overrides()["MONEY_PRINTER_URL"] == "http://runtime:8098"


# ---------------------------------------------------------------------------
# P7: the Worker reports its own effective configuration, and CORE believes it
# ---------------------------------------------------------------------------


def _worker_registry(monkeypatch, registry) -> None:
    """Point the Worker's own registry at the engine this process resolved.

    A node builds its registry when it imports the provider layer, so the test has to
    replace that binding rather than the module attribute the factory reads.
    """
    from worker import service as worker_service

    monkeypatch.setattr(worker_service, "providers", type("_P", (), {
        "video_engine": staticmethod(lambda: registry["engine"]),
    }))


def test_a_worker_reports_the_effective_engine_of_its_own_executor(registry, monkeypatch):
    """P7: звіт про готовність надходить із процесу виконавця, а не з CORE.

    CORE не може нікчемно підтвердити конфігурацію іншого вузла, тому вузол сам
    повідомляє несекретні поля своєї конфігурації та результат перевірки готовності на
    власному процесі. Саме цей звіт і є тим, що CORE порівнює зі snapshot спроби.
    """
    from worker.service import claim_video_engine_state

    _worker_registry(monkeypatch, registry)
    registry["engine"] = _Native()
    monkeypatch.setenv("MONEY_PRINTER_URL", "")
    native = claim_video_engine_state()
    assert native["engine_id"] == "native"
    assert native["ready"] is True
    assert native["reason"] is None
    assert "endpoint_reference" not in native, "Vertep Native не має зовнішніх полів"

    registry["engine"] = _EnvRuntime(ready=True)
    switch_provider("video_engine", "money-printer", actor="test",
                    endpoint="http://runtime:8098")
    external = claim_video_engine_state()
    assert external["engine_id"] == "money-printer"
    assert external["ready"] is True
    assert external["endpoint_reference"] == {
        "env": "MONEY_PRINTER_URL", "endpoint": "http://runtime:8098", "configured": True}
    assert external["config_revision"] == engine_config.engine_config_revision()
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "super-secret-token")
    assert "super-secret-token" not in json.dumps(claim_video_engine_state())


def test_a_worker_reports_an_unready_runtime_instead_of_claiming_readiness(registry,
                                                                          monkeypatch):
    from worker.service import claim_video_engine_state

    _worker_registry(monkeypatch, registry)
    registry["engine"] = _EnvRuntime(ready=False)
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")

    report = claim_video_engine_state()

    assert report["ready"] is False
    assert report["reason"] == "upstream_unreachable"


def test_a_worker_whose_engine_state_cannot_be_read_reports_not_ready(registry, monkeypatch):
    """Незрозумілий стан вузла — це відмова, а не мовчазливе «готово»."""
    from worker import service as worker_service

    def _broken():
        raise RuntimeError("provider registry is unreadable")

    monkeypatch.setattr(worker_service, "providers", type("_P", (), {
        "video_engine": staticmethod(_broken),
    }))

    report = worker_service.claim_video_engine_state()

    assert report["ready"] is False
    assert report["engine_id"] == "unknown"
    assert report["reason"].startswith("engine_state_error:")