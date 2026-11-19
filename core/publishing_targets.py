"""Publication targets of a brand (var.0.0.4.86, #486).

A Telegram channel is a publication target of a brand, the same as the targets
of the other platforms: it is a ``Channel`` of type ``telegram`` on the brand.
There is no installation-wide Telegram channel any more, and no automatic
migration from the former ``telegram_channel_id`` setting (owner decision): until
a brand has an enabled Telegram channel, publishing to Telegram is not configured.
"""

from __future__ import annotations


def brand_channel(brand_id: str | None, channel_type: str, target: str | None = None,
                  channel_id: str | None = None):
    """The enabled channel record of the brand a publication goes to (#502)."""
    from .brand_store import list_channels
    if not brand_id:
        return None
    candidates = [channel for channel in list_channels(brand_id)
                  if channel.enabled and channel.channel_type == channel_type]
    for wanted in (channel_id, target):
        if wanted:
            for channel in candidates:
                if wanted in (channel.channel_id, channel.target):
                    return channel
    return candidates[0] if candidates else None


def brand_targets(brand_id: str | None, channel_type: str) -> list[str]:
    """Targets of the enabled channels of ``channel_type`` on a brand, in a stable order."""
    from .brand_store import list_channels

    if not brand_id:
        return []
    return [channel.target for channel in list_channels(brand_id)
            if channel.enabled and channel.channel_type == channel_type and channel.target]


def all_targets(channel_type: str) -> list[str]:
    """Enabled targets of ``channel_type`` over all brands (for readiness)."""
    from .brand_store import list_channels

    seen: list[str] = []
    for channel in list_channels():
        if channel.enabled and channel.channel_type == channel_type and channel.target \
                and channel.target not in seen:
            seen.append(channel.target)
    return seen


def telegram_target(job, explicit: str | None = None) -> str:
    """The Telegram channel a publication of ``job`` goes to, or ``""``.

    An explicitly chosen brand channel wins; a retry keeps the target of the
    recorded publish intent; otherwise the first enabled Telegram channel of the
    job's brand is used.
    """
    if explicit:
        return explicit
    recorded = ((getattr(job, "publish_intent", None) or {}).get("telegram") or {}).get("target")
    targets = brand_targets(getattr(job, "brand_id", None), "telegram")
    if recorded and recorded in targets:
        return recorded
    return targets[0] if targets else ""
