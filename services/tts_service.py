"""Локальний TTS runtime на базі espeak-ng."""

import base64
import os
import re
import shutil
import subprocess

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep TTS", version="1")
VOICE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")

# espeak-ng ships its voice table under this directory; the list is read once
# at startup and cached so /voices and /synthesize agree on what is supported.
_VOICE_DATA_DIR = os.environ.get("ESPEAK_VOICE_DIR", "/usr/lib/espeak-ng-data/voices")


def _available_voices() -> list[str]:
    """Enumerate the espeak-ng voices actually installed on this host.

    Returns an empty list when the voice table cannot be read, so the runtime
    never advertises a catalog it cannot back (Issue i.0.0.1.5: a missing
    catalog must not be silently treated as "accepts anything").
    """
    directory = _VOICE_DATA_DIR
    if not os.path.isdir(directory):
        return []
    voices: list[str] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".idx") and not name.endswith(".list"):
            continue
        voice = name.rsplit(".", 1)[0]
        if voice:
            voices.append(voice)
    return voices


def _available_languages(installed: list[str] | None = None) -> list[str]:
    """Derive the language tags the espeak-ng runtime can actually synthesize.

    Derived from the installed voice identifiers (``en-us`` → ``en``): the
    runtime has no separate language table, so the voice table is the source
    of truth (Issue i.0.0.1.5).  Returns an empty list when no voice table is
    available, so the runtime never advertises languages it cannot back.
    """
    voices = installed if installed is not None else _available_voices()
    languages: set[str] = set()
    for voice in voices:
        tag = str(voice).strip().lower().split("_")[0].split("-")[0]
        if tag:
            languages.add(tag)
    return sorted(languages)


VOICES = _available_voices()


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    voice: str = Field(default="uk", max_length=32)
    speed: int = Field(default=150, ge=80, le=450)
    # Optional character voice dimensions the espeak runtime does not gate on
    # directly: accepted so the Voice Worker can forward the full character
    # voice config (provider/model/language) in one synthesis request without
    # the runtime rejecting fields it legitimately ignores.  ``language`` is
    # validated against the installed voice table when it is pinned; ``model``
    # and ``provider`` are accepted as-is because the espeak engine has no
    # model/provider table of its own — provider/model readiness is enforced
    # at dispatch time in CORE via the worker voice catalog instead.
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)


@app.get("/health")
def health() -> dict:
    executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
    if not executable:
        raise HTTPException(503, "espeak-ng недоступний")
    return {"status": "HEALTHY", "engine": "espeak-ng"}


@app.get("/voices")
def voices() -> dict:
    """Runtime voice catalog advertised to CORE in the worker heartbeat.

    Issue i.0.0.1.5: the espeak service previously exposed only
    text/voice/speed and had no /voices endpoint, so CORE could never reconcile
    voice/provider/model/language readiness with the actual runtime.  This
    endpoint returns exactly the voices espeak-ng can actually synthesize, so a
    worker that declares this catalog is gated on it and an unsupported
    combination is refused at dispatch time instead of at synthesis time.
    """
    installed = VOICES or _available_voices()
    languages = _available_languages(installed)
    return {
        "voices": [{"id": voice, "engine": "espeak-ng"} for voice in installed],
        "models": ["espeak-ng"],
        "languages": languages,
        # Issue #198/#165.1: advertise ONLY the real espeak-ng runtime.  "mock"
        # and "none" are not synthesizers — advertising them let CORE's
        # fail-closed catalog gate accept a voice task that the worker could
        # never actually execute, and the Job later failed at synthesis time
        # instead of being refused at dispatch.  A declared catalog is a
        # contract: every advertised value must be backed by the runtime.
        "providers": ["espeak-ng"],
        "speed_range": {"min": 80, "max": 450},
        "engine": "espeak-ng",
    }


def _validate_voice(voice: str) -> None:
    if not VOICE_RE.fullmatch(voice):
        raise HTTPException(422, "Некоректний ідентифікатор голосу")
    installed = VOICES or _available_voices()
    if installed and voice not in installed:
        raise HTTPException(422, f"Голос {voice!r} не підтримується цим espeak runtime")


def _validate_language(language: str | None) -> None:
    """Reject a pinned language the runtime cannot synthesize (Issue i.0.0.1.5).

    The requirement text demands that unsupported combinations be rejected
    while the runtime integration stays on the stock espeak engine.  espeak-ng
    has no separate language table, so support is derived from the installed
    voice identifiers: a language is supported exactly when at least one
    installed voice maps to it.  An unpinned language (None/empty) is not
    gated; a pinned one that the runtime cannot back is refused with 422
    instead of failing later at synthesis time.
    """
    if not language:
        return
    installed = VOICES or _available_voices()
    if not installed:
        # No voice table to contradict the request: fail-closed at dispatch
        # time in CORE via the empty catalog, not here at synthesis time.
        return
    supported = set(_available_languages(installed))
    if str(language).strip().lower() not in supported:
        raise HTTPException(422, f"Мова {language!r} не підтримується цим espeak runtime")


@app.post("/synthesize")
def synthesize(request: SynthesisRequest) -> dict:
    _validate_voice(request.voice)
    _validate_language(request.language)
    executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
    if not executable:
        raise HTTPException(503, "espeak-ng недоступний")
    try:
        result = subprocess.run(
            [executable, "--stdout", "-v", request.voice, "-s", str(request.speed), request.text],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise HTTPException(502, "TTS engine не зміг синтезувати аудіо") from error
    if not result.stdout.startswith(b"RIFF"):
        raise HTTPException(502, "TTS engine повернув некоректний WAV")
    return {"audio_base64": base64.b64encode(result.stdout).decode("ascii"),
            "mime_type": "audio/wav", "engine": "espeak-ng", "voice": request.voice}