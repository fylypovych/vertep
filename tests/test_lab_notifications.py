"""Notification importance settings: one store for Web and Telegram (Issue #251, U3)."""

import pytest

from lab.notifications import DEFAULTS, NotificationSettings, SettingsError, validate
from lab.policy import PolicyGate


def gate_for(tmp_path) -> PolicyGate:
    return PolicyGate(tmp_path / "state")


def test_defaults_cover_web_and_telegram(tmp_path):
    settings = NotificationSettings(gate_for(tmp_path))
    assert settings.load() == DEFAULTS
    assert set(settings.load()) == {"web", "telegram"}


def test_update_persists_and_is_audited(tmp_path):
    gate = gate_for(tmp_path)
    settings = NotificationSettings(gate)
    outcome = settings.update({"telegram": {"info": True}},
                              actor="owner", reason="панель показує info")
    assert outcome["settings"]["telegram"]["info"] is True
    assert outcome["settings"]["web"]["info"] is True  # untouched channel keeps defaults

    reloaded = NotificationSettings(gate_for(tmp_path)).load()
    assert reloaded["telegram"]["info"] is True

    trail = [r for r in gate.trail.records()
             if r.get("action") == "notification_settings:update"]
    assert trail and trail[-1]["actor"] == "owner"
    assert trail[-1]["decision"] == "UPDATED"


def test_unknown_channel_or_level_is_refused():
    with pytest.raises(SettingsError, match="unknown channel"):
        validate({"email": {"info": True}})
    with pytest.raises(SettingsError, match="unknown level"):
        validate({"web": {"fatal": True}})
    with pytest.raises(SettingsError, match="must be an object"):
        validate({"web": "yes"})


def test_corrupt_store_falls_back_to_defaults(tmp_path):
    gate = gate_for(tmp_path)
    (tmp_path / "state" / "notifications.json").parent.mkdir(parents=True,
                                                             exist_ok=True)
    (tmp_path / "state" / "notifications.json").write_text("{broken", encoding="utf-8")
    assert NotificationSettings(gate).load() == DEFAULTS


def test_settings_are_never_silently_widened(tmp_path):
    # Partial payloads keep the remaining levels at the documented defaults,
    # so a panel bug cannot silently enable every channel.
    settings = NotificationSettings(gate_for(tmp_path))
    normalised = validate({"web": {"critical": False}})
    assert normalised["web"]["info"] is True
    assert normalised["web"]["critical"] is False
    assert settings.load() == DEFAULTS
