"""Notification importance settings shared by the Web panel and Telegram.

One store decides which event importance levels are delivered per channel, so
the Angular panel and the Telegram channel cannot drift apart (Issue #251, U3;
Issue #107 approved UX: importance settings in Web + Telegram).  Reads are open
like every other lab state read; a write is an owner act with the same
``X-Lab-Owner-Token`` proof as grants and is audited through the one trail.
"""

from datetime import datetime, timezone

from .policy import PolicyGate
from .state_store import read_json, write_json

CHANNELS: tuple = ("web", "telegram")
LEVELS: tuple = ("info", "important", "critical")

DEFAULTS: dict = {
    "web": {"info": True, "important": True, "critical": True},
    "telegram": {"info": False, "important": True, "critical": True},
}


class SettingsError(ValueError):
    """A settings payload violates the notification contract."""


def validate(settings) -> dict:
    """Return a normalised settings document or raise :class:`SettingsError`."""
    if not isinstance(settings, dict):
        raise SettingsError("settings must be an object")
    unknown = sorted(set(settings) - set(CHANNELS))
    if unknown:
        raise SettingsError(f"unknown channel(s): {', '.join(unknown)}")
    normalised: dict = {}
    for channel in CHANNELS:
        payload = settings.get(channel, {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise SettingsError(f"channel {channel} must be an object")
        unknown_levels = sorted(set(payload) - set(LEVELS))
        if unknown_levels:
            raise SettingsError(f"channel {channel}: unknown level(s) "
                                + ", ".join(unknown_levels))
        normalised[channel] = {
            level: bool(payload.get(level, DEFAULTS[channel][level]))
            for level in LEVELS
        }
    return normalised


class NotificationSettings:
    """Durable per-channel importance settings behind the shared gate."""

    def __init__(self, gate: PolicyGate, *, state_dir=None):
        self.gate = gate
        self.path = (state_dir or gate.state_dir) / "notifications.json"

    def load(self) -> dict:
        stored = read_json(self.path, default=None)
        if not isinstance(stored, dict):
            return {channel: dict(levels) for channel, levels in DEFAULTS.items()}
        try:
            return validate(stored)
        except SettingsError:
            # A corrupt store must not silently notify more than the owner
            # approved: fall back to defaults, which are the documented ones.
            return {channel: dict(levels) for channel, levels in DEFAULTS.items()}

    def update(self, settings, *, actor: str = "owner",
               reason: str = "") -> dict:
        """Validate, persist and audit a new settings document."""
        normalised = validate(settings)
        write_json(self.path, normalised)
        self.gate.trail.append(actor=actor, action="notification_settings:update",
                               subject=",".join(CHANNELS),
                               decision="UPDATED",
                               rule="owner_settings",
                               reason=reason or
                               "; ".join(f"{channel}: "
                                         + ",".join(level for level in LEVELS
                                                    if normalised[channel][level])
                                         for channel in CHANNELS))
        return {"settings": normalised, "updated_at": datetime.now(timezone.utc).isoformat()}
