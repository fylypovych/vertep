"""Role profiles parsed from ``lab/role_profiles.md`` — the single source of truth.

``AGENTS.md`` §4.6 keeps authorisation inside one policy gate; the gate has to
know what a role may do on its own.  The executable permission sets live in
``lab.policy`` (``STANDING_PERMISSIONS`` / ``GATED_ACTIONS`` / ``FORBIDDEN_ACTIONS``)
and the per-role projection lives in the markdown document the owner approved.
This module turns that document into validated :class:`RoleProfile` objects so
tests, CLI and the orchestrator never carry an inline copy of the table.

The parse is strict and fail-closed: a malformed document, an unknown action or
a role that claims a forbidden action raises :class:`RoleProfilesError` at
import time instead of silently widening anyone's rights.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_PROFILES_PATH = Path(__file__).with_name("role_profiles.md")
_SECTION = re.compile(r"^###\s+\d+\.\s+([a-z_]+)\b")
_BULLET = re.compile(r"^- `([a-z_]+)`")


class RoleProfilesError(RuntimeError):
    """Raised when ``role_profiles.md`` cannot be parsed or fails validation."""


@dataclass(frozen=True)
class RoleProfile:
    """Standing and gated actions of one laboratory role."""

    name: str
    standing: frozenset[str] = field(default_factory=frozenset)
    gated: frozenset[str] = field(default_factory=frozenset)

    @property
    def read_only(self) -> bool:
        """A role without ``local_fix`` may observe but never change the copy."""
        return "local_fix" not in self.standing


def _blocks(section_lines: list[str]) -> dict[str, frozenset[str]]:
    blocks: dict[str, set[str]] = {"STANDING_PERMISSIONS": set(), "GATED_ACTIONS": set()}
    current: str | None = None
    for line in section_lines:
        stripped = line.strip()
        if stripped.startswith("**STANDING_PERMISSIONS"):
            current = "STANDING_PERMISSIONS"
            continue
        if stripped.startswith("**GATED_ACTIONS"):
            current = "GATED_ACTIONS"
            continue
        if stripped.startswith("**"):
            current = None
            continue
        if current is None or not stripped.startswith("- "):
            continue
        match = _BULLET.match(stripped)
        if match:
            blocks[current].add(match.group(1))
    return {name: frozenset(actions) for name, actions in blocks.items()}


def load_role_profiles(path: Path | str | None = None, *,
                       standing: frozenset[str],
                       gated: frozenset[str],
                       forbidden: frozenset[str]) -> dict[str, RoleProfile]:
    """Parse and validate the approved role table.

    ``standing``/``gated``/``forbidden`` come from :mod:`lab.policy`; they are
    passed in to keep this module dependency-free (no import cycle).
    """
    source = Path(path) if path else _PROFILES_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise RoleProfilesError(f"cannot read {source}: {error}") from error

    profiles: dict[str, RoleProfile] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _SECTION.match(lines[index])
        if not match:
            index += 1
            continue
        name = match.group(1)
        index += 1
        section: list[str] = []
        while index < len(lines):
            if lines[index].startswith("### ") or lines[index].startswith("## "):
                break
            section.append(lines[index])
            index += 1
        if name in profiles:
            raise RoleProfilesError(f"duplicate role section: {name}")
        blocks = _blocks(section)
        profile = RoleProfile(name=name, standing=blocks["STANDING_PERMISSIONS"],
                              gated=blocks["GATED_ACTIONS"])
        _validate(profile, standing, gated, forbidden)
        profiles[name] = profile
    return profiles


def _validate(profile: RoleProfile, standing: frozenset[str],
              gated: frozenset[str], forbidden: frozenset[str]) -> None:
    if not profile.standing:
        raise RoleProfilesError(f"{profile.name}: STANDING_PERMISSIONS is empty")
    unknown_standing = profile.standing - standing
    unknown_gated = profile.gated - gated
    if unknown_standing:
        raise RoleProfilesError(
            f"{profile.name}: unknown standing actions {sorted(unknown_standing)}")
    if unknown_gated:
        raise RoleProfilesError(
            f"{profile.name}: unknown gated actions {sorted(unknown_gated)}")
    forbidden_claim = (profile.standing | profile.gated) & forbidden
    if forbidden_claim:
        raise RoleProfilesError(
            f"{profile.name}: forbidden actions claimed {sorted(forbidden_claim)}")
    overlap = profile.standing & profile.gated
    if overlap:
        raise RoleProfilesError(
            f"{profile.name}: actions both standing and gated {sorted(overlap)}")
