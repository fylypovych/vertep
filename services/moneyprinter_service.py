"""Vertep wrapper around the pinned MoneyPrinterTurbo runtime (Issue #122 P2).

The pinned upstream API is an implementation detail: it binds loopback inside the
container and this wrapper is the only externally reachable endpoint. The wrapper
answers the three questions the dispatch layer needs before it may route work to
the external engine:

``GET /health``     cheap readiness — can this wrapper serve at all?
``GET /runtime``    the pinned runtime snapshot (commit, versions, dependencies).
``GET /self-test``  the full eligibility gate of Issue #122 §9.12.

Every failure mode is reported as an explicit, stable reason code and the endpoint
answers 503. Nothing here falls back to another engine: an unverifiable runtime
must be refused, never silently skipped (Issue #122 §9.14).
"""

from __future__ import annotations

import array
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from adapters.providers.base import (
    REASON_FFMPEG_UNAVAILABLE,
    REASON_INVENTORY_UNVERIFIED,
    REASON_MEDIA_PIPELINE_FAILED,
    REASON_SCHEMA_UNSUPPORTED,
    REASON_SNAPSHOT_MISMATCH,
    REASON_SUBMIT_RECORD_UNREADABLE,
    REASON_SUBMIT_UNKNOWN,
    REASON_UPSTREAM_UNAUTHENTICATED,
    REASON_UPSTREAM_UNREACHABLE,
    REASON_VOICE_STAGING_FAILED,
    REASON_VOICE_STAGING_UNSUPPORTED,
    FIXED_SUBMIT_FIELDS,
    REQUIRED_SUBMIT_FIELDS,
)
from adapters.providers.runtime_manifest import (
    DEFAULT_INVENTORY_PATH,
    INVENTORY_PATH_ENV,
    RuntimeManifestError,
    generate_sbom,
    load_manifest,
    verify_inventory,
)

SERVICE_NAME = "moneyprinter"
WRAPPER_API_VERSION = "v1"

UPSTREAM_URL_ENV = "MONEYPRINTER_UPSTREAM_URL"
API_KEY_FILE_ENV = "MONEYPRINTER_API_KEY_FILE"
IMAGE_DIGEST_ENV = "VERTEP_MONEYPRINTER_IMAGE_DIGEST"
INVENTORY_ROOT_ENV = "MONEYPRINTER_RUNTIME_ROOT"

DEFAULT_UPSTREAM_URL = "http://127.0.0.1:8080"
DEFAULT_API_KEY_FILE = "/run/secrets/moneyprinter_api_key"

# Reason codes and the submit-field contract come from adapters.providers.base so the
# wrapper and the engine classify a refusal identically.


class SelfTestFailure(RuntimeError):
    """A self-test check that must fail closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _inventory_path() -> Path:
    configured = os.getenv(INVENTORY_PATH_ENV, "").strip()
    return Path(configured or DEFAULT_INVENTORY_PATH)


def _inventory_root() -> Path:
    configured = os.getenv(INVENTORY_ROOT_ENV, "").strip()
    if configured:
        return Path(configured)
    return _inventory_path().parent


def _upstream_url() -> str:
    return os.getenv(UPSTREAM_URL_ENV, "").strip() or DEFAULT_UPSTREAM_URL


def _declared_image_digest() -> str:
    return os.getenv(IMAGE_DIGEST_ENV, "").strip()


def read_api_key(path: Path | None = None) -> str:
    """Read the runtime API key. An empty key must never be tolerated."""
    target = Path(path or os.getenv(API_KEY_FILE_ENV, "").strip() or DEFAULT_API_KEY_FILE)
    if not target.is_file():
        raise SelfTestFailure(
            REASON_UPSTREAM_UNAUTHENTICATED,
            f"runtime API key file is not readable: {target}",
        )
    value = target.read_text(encoding="utf-8").strip()
    if not value:
        raise SelfTestFailure(
            REASON_UPSTREAM_UNAUTHENTICATED,
            "runtime API key is empty; refusing to start an unauthenticated API",
        )
    return value


def headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


def verify_runtime_snapshot(expected_commit: str, expected_bridge_version: str,
                            expected_bridge_schema_version: str) -> dict:
    """Verify the pinned inventory and the deployed image digest.

    An unverified inventory or a missing image digest is a refusal, not a warning:
    Issue #122 §9.15 requires the snapshot to fix the pinned image digest, the
    bridge/API version and the upstream commit together.
    """
    digest = _declared_image_digest()
    if not digest:
        raise SelfTestFailure(
            REASON_SNAPSHOT_MISMATCH,
            f"{IMAGE_DIGEST_ENV} is not set; the deployed runtime digest is unknown",
        )
    if not (digest.startswith("sha256:") and len(digest) == len("sha256:") + 64):
        raise SelfTestFailure(
            REASON_SNAPSHOT_MISMATCH,
            f"{IMAGE_DIGEST_ENV} is not an immutable image digest",
        )

    try:
        manifest = verify_inventory(
            _inventory_path(),
            root=_inventory_root(),
            expected_commit=expected_commit,
            expected_bridge_version=expected_bridge_version,
            expected_bridge_schema_version=expected_bridge_schema_version,
        )
    except RuntimeManifestError as error:
        raise SelfTestFailure(REASON_INVENTORY_UNVERIFIED, str(error)) from error

    return {
        "image_digest": digest,
        "bridge_version": manifest.bridge_version,
        "bridge_schema_version": manifest.bridge_schema_version,
        "wrapper_api_version": WRAPPER_API_VERSION,
        "upstream_commit": manifest.upstream_commit,
        "upstream_reference": manifest.upstream_reference,
        "upstream_repository": manifest.upstream_repository,
        "upstream_version": manifest.upstream_version,
        "dependencies": sorted(manifest.dependencies),
        "dependency_count": len(manifest.dependencies),
        "inventory_digest": manifest.lock_digest,
    }


def check_upstream_authenticated(client: httpx.Client, api_key: str) -> None:
    """Probe the pinned upstream API exactly the way dispatch will use it."""
    base = _upstream_url()
    try:
        unauthorized = client.get(f"{base}/api/v1/tasks", params={"page": 1, "page_size": 1},
                                  timeout=10)
    except httpx.HTTPError as error:
        raise SelfTestFailure(
            REASON_UPSTREAM_UNREACHABLE,
            f"pinned upstream API is unreachable at {base}: {type(error).__name__}",
        ) from error

    if unauthorized.status_code != 401:
        raise SelfTestFailure(
            REASON_UPSTREAM_UNAUTHENTICATED,
            "pinned upstream API does not enforce x-api-key authentication "
            f"(answered {unauthorized.status_code} without a key)",
        )

    try:
        authorized = client.get(
            f"{base}/api/v1/tasks",
            params={"page": 1, "page_size": 1},
            headers=headers(api_key),
            timeout=10,
        )
        authorized.raise_for_status()
        payload = authorized.json()
    except (httpx.HTTPError, ValueError) as error:
        raise SelfTestFailure(
            REASON_UPSTREAM_UNREACHABLE,
            f"authenticated upstream probe failed: {type(error).__name__}",
        ) from error

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            "upstream task listing response does not match the pinned schema",
        )


def _annotation_name(annotation) -> str:
    """Name of a field annotation that never raises.

    ``video_materials`` is an ``Optional[List[...]]`` and ``video_aspect`` an
    ``Optional[...]``, whose ``__name__`` is not guaranteed to exist. The schema proof
    must describe the pinned fields, so an unrepresentable annotation falls back to its
    text instead of turning a healthy runtime into an error.
    """
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) and name else str(annotation)


def check_submit_schema() -> dict:
    """Confirm the pinned upstream still declares every field the bridge submits.

    The fixed §9.4 set is verified, not only the fields that can start a task: the pinned
    request model ignores unknown keys, so a dropped compose field would otherwise be
    applied with the runtime's own default and the approved contract would be reported as
    honoured (§9.4, §9.12).
    """
    try:
        from app.models.schema import TaskVideoRequest
    except ImportError as error:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            f"pinned upstream schema is not importable: {error}",
        ) from error

    fields = getattr(TaskVideoRequest, "model_fields", None)
    if not fields:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            "upstream TaskVideoRequest exposes no model fields",
        )

    missing = [name for name in FIXED_SUBMIT_FIELDS if name not in fields]
    if missing:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            f"upstream TaskVideoRequest is missing required fields: {', '.join(missing)}",
        )
    return {
        name: _annotation_name(fields[name].annotation)
        for name in FIXED_SUBMIT_FIELDS
    }


def task_local_voice_capability() -> dict:
    """Can the approved voice reach the pinned engine without its own TTS?

    Issue #122 §9.3 row 5 requires the approved voice to arrive as
    ``custom_audio_file`` and §9.6 forbids the runtime from synthesising
    narration. The pinned upstream resolves that field inside the task directory
    (``app/services/task.py::resolve_custom_audio_file``) and refuses a
    server-side path for API callers; the task id is generated server-side at
    submit time.

    The wrapper closes exactly that gap: it stages the approved bytes itself and
    writes them into the task directory right after submit, then verifies the
    pinned resolver accepts the file. ``POST /api/v1/audio`` is **not** such a
    route: it runs the pipeline ``stop_at="audio"``, i.e. it synthesises
    narration with the pinned TTS, which §9.6 forbids, so it must never be
    mistaken for staging.

    The safety property does not depend on winning a race: the submit always
    carries a non-empty task-local ``custom_audio_file``, so the pinned pipeline
    either resolves the staged file or fails the task at the audio stage. There
    is no path from an accepted submit to runtime-generated narration.
    """
    try:
        from app.services.task import resolve_custom_audio_file
    except ImportError as error:
        return {
            "task_local_voice": False,
            "reason": REASON_SCHEMA_UNSUPPORTED,
            "detail": f"pinned audio resolution is not importable: {error}",
        }

    parameter = inspect.signature(resolve_custom_audio_file).parameters
    task_local_only = not parameter["allow_server_file_input"].default
    supports_staging = task_local_only or bool(_audio_upload_routes())
    return {
        "task_local_voice": supports_staging,
        "reason": None if supports_staging else REASON_VOICE_STAGING_UNSUPPORTED,
        "routes": sorted({VOICE_STAGING_ROUTE, *_audio_upload_routes()}),
        "task_local_resolution_only": task_local_only,
        "detail": (
            "the wrapper stages the approved voice into the pinned task directory"
            if supports_staging else
            "the pinned runtime resolves custom_audio_file neither task-locally nor "
            "through an upload route, so an approved voice file cannot be delivered"
        ),
    }


def _audio_upload_routes() -> list[str]:
    """Pinned API routes that store an uploaded audio file for a task.

    A route qualifies only when its handler accepts an uploaded file
    (``UploadFile``); a route that generates audio from the submitted script
    (``stop_at="audio"``) is not voice staging and must not be mistaken for it.
    """
    from fastapi import UploadFile

    routes: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1") or "audio" not in path:
            continue
        handler = getattr(route, "endpoint", None)
        if handler is None:
            continue
        try:
            parameters = inspect.signature(handler).parameters
        except (TypeError, ValueError):
            continue
        if any(
            isinstance(parameter.annotation, type)
            and issubclass(parameter.annotation, UploadFile)
            for parameter in parameters.values()
        ):
            routes.append(path)
    return routes


# ---------------------------------------------------------------------------
# Approved voice staging (Issue #122 §9.3 row 5, §9.6)
#
# The pinned resolver only accepts a path inside the task directory, and the task
# id is generated by the upstream at submit time. The wrapper therefore stages the
# approved bytes itself and places them in the task directory immediately after
# submit. Because the submitted ``custom_audio_file`` is never empty, the pinned
# pipeline either resolves that file or fails the task at the audio stage: it can
# never fall back to its own TTS, so the staging race cannot produce a silent
# substitution.
# ---------------------------------------------------------------------------

VOICE_STAGING_ROUTE = "/api/v1/voice"
VOICE_MARKER_PREFIX = "vertep-voice:"
VOICE_TASK_FILENAME = "vertep-voice"
VOICE_DIR_ENV = "VERTEP_MONEYPRINTER_VOICE_DIR"
DEFAULT_VOICE_DIR = "/opt/moneyprinter/.vertep-voice"
ALLOWED_VOICE_SUFFIXES = frozenset({".wav", ".mp3", ".aac", ".m4a", ".ogg"})
MAX_VOICE_BYTES = 64 * 1024 * 1024


def _voice_dir() -> Path:
    return Path(os.getenv(VOICE_DIR_ENV, DEFAULT_VOICE_DIR))


# Durable submit keys (Issue #122 §5, §9.8).
#
# The pinned upstream has no idempotency key, so the wrapper owns the record: it
# writes the key BEFORE it contacts the upstream and fills in the created task id
# afterwards. A repeat submit with the same key can therefore never create a
# second upstream task, and a lost response is reconciled into either a real task
# id or an explicit UNKNOWN — never a silent success.
SUBMIT_RECORD_DIR_ENV = "VERTEP_MONEYPRINTER_SUBMIT_RECORDS"
DEFAULT_SUBMIT_RECORD_DIR = "/opt/moneyprinter/storage/vertep-submits"


def _submit_record_dir() -> Path:
    return Path(os.getenv(SUBMIT_RECORD_DIR_ENV, DEFAULT_SUBMIT_RECORD_DIR))


def _submit_record_path(submit_key: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", submit_key):
        raise HTTPException(status_code=400, detail="invalid submit key")
    return _submit_record_dir() / f"{submit_key}.json"


def _submit_record(submit_key: str) -> dict | None:
    """Read the durable submit record of a key.

    A missing record means the key was never used. A record that exists but cannot be
    read or parsed is a different situation: treating it as absent would let a
    corrupted record become a second upstream POST for one approved render, so it
    fails closed with an explicit reason instead (Issue #122 P6).
    """
    path = _submit_record_path(submit_key)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SelfTestFailure(
            REASON_SUBMIT_RECORD_UNREADABLE,
            f"submit record {submit_key!r} exists but is unreadable: {error}",
        ) from error
    try:
        record = json.loads(raw)
    except ValueError as error:
        raise SelfTestFailure(
            REASON_SUBMIT_RECORD_UNREADABLE,
            f"submit record {submit_key!r} is corrupted and cannot be trusted",
        ) from error
    if not isinstance(record, dict) or record.get("submit_key") != submit_key:
        raise SelfTestFailure(
            REASON_SUBMIT_RECORD_UNREADABLE,
            f"submit record {submit_key!r} does not describe this submit key",
        )
    return record


def _remember_submit(submit_key: str, *, task_id: str | None,
                     marker: str | None = None, state: str | None = None) -> None:
    """Persist the durable state of one submit key (§5, §9.8).

    ``state`` distinguishes the four situations the caller has to tell apart:

    ``submitting``  the key is reserved, the upstream task is not confirmed yet;
    ``submitted``   the upstream task exists, the approved voice is not staged yet;
    ``staged``      the approved voice is verified inside the pinned task directory;
    ``refused``     the attempt was aborted and must not be resubmitted under this key.
    """
    if not submit_key:
        return
    path = _submit_record_path(submit_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = state or ("submitted" if task_id else "submitting")
    payload = {"submit_key": submit_key, "task_id": task_id,
               "voice_marker": marker, "state": resolved}
    temporary = path.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _forget_submit(submit_key: str) -> None:
    """Release a key whose submit the upstream definitively refused.

    Only a refusal that proves no upstream task exists may release a key. An unknown
    outcome keeps its record, because the upstream may already hold the work (Issue
    #122 §5). A record that cannot be removed is left in place: the next submit then
    fails closed instead of risking a second task.
    """
    if not submit_key:
        return
    try:
        _submit_record_path(submit_key).unlink(missing_ok=True)
    except OSError:
        pass


def marker_suffix(marker: str) -> str:
    """File suffix of a staged-voice marker."""
    suffix = Path(marker).suffix.lower()
    if suffix not in ALLOWED_VOICE_SUFFIXES:
        raise HTTPException(status_code=400, detail="invalid staged voice reference")
    return suffix


def _require_submit_invariants(payload: dict) -> str:
    """Check the pinned contract and return the staged-voice marker.

    Every refusal here happens before the upstream is contacted, so an attempt
    that violates §9.4/§9.6 never becomes a paid or unapproved upstream task.
    """
    script = str(payload.get("video_script") or "").strip()
    if not script:
        # An empty script makes the pinned runtime generate narration text (LLM).
        raise HTTPException(status_code=400, detail="approved script is required")
    marker = str(payload.get("custom_audio_file") or "").strip()
    if not marker.startswith(VOICE_MARKER_PREFIX):
        # Empty would start the pinned TTS; anything else would be a path CORE
        # cannot expose to this container.
        raise HTTPException(
            status_code=400, detail="approved voice must be staged before submit",
        )
    marker_suffix(marker)
    materials = payload.get("video_materials")
    if not isinstance(materials, list) or not materials:
        raise HTTPException(status_code=400, detail="scene materials are required")
    for material in materials:
        reference = str((material or {}).get("url") or "")
        if not reference or "/" in reference or "\\" in reference or "://" in reference:
            raise HTTPException(
                status_code=400, detail="scene material must be an uploaded storage key",
            )
    bgm = str(payload.get("bgm_file") or "")
    if bgm and ("://" in bgm or "/" in bgm or "\\" in bgm):
        raise HTTPException(status_code=400, detail="bgm must not be a remote path")
    return marker


def _submitted_task_id(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError as error:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED, "upstream submit returned a non-JSON body",
        ) from error
    payload = data.get("data") if isinstance(data, dict) else None
    task_id = payload.get("task_id") if isinstance(payload, dict) else None
    if not isinstance(task_id, str) or not task_id.strip():
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED, "upstream submit returned no data.task_id",
        )
    return task_id


def _verified_staged_voice(marker: str) -> bytes:
    """The approved voice bytes this marker names, proved before anything else happens.

    The pinned runtime resolves ``custom_audio_file`` inside a task directory that only
    exists after the submit, so the bytes cannot be placed there first. They can still be
    *proved* first: a missing or tampered staged voice is refused before the upstream is
    contacted, so no render is ever created that could only be narrated by the pinned TTS
    (Issue #122 P3, §9.6).
    """
    digest = marker[len(VOICE_MARKER_PREFIX):-len(marker_suffix(marker))]
    source = _voice_dir() / f"{digest}{marker_suffix(marker)}"
    if not source.is_file():
        raise SelfTestFailure(
            REASON_VOICE_STAGING_UNSUPPORTED,
            "staged voice file is missing; the attempt is refused",
        )
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise SelfTestFailure(
            REASON_VOICE_STAGING_UNSUPPORTED,
            "staged voice file does not match its content address",
        )
    return payload


def _stage_task_voice(task_id: str, marker: str) -> Path:
    """Place the approved voice inside the pinned task directory."""
    payload = _verified_staged_voice(marker)

    from app.utils import utils

    task_path = Path(utils.task_dir(task_id)) / f"{VOICE_TASK_FILENAME}{marker_suffix(marker)}"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = task_path.with_suffix(task_path.suffix + ".part")
    temporary.write_bytes(payload)
    temporary.replace(task_path)

    from app.services.task import resolve_custom_audio_file

    resolved = Path(
        resolve_custom_audio_file(task_id, task_path.name, allow_server_file_input=False)
    )
    if resolved != task_path or not resolved.is_file():
        raise SelfTestFailure(
            REASON_VOICE_STAGING_UNSUPPORTED,
            "the pinned resolver did not accept the staged voice file",
        )
    return resolved


def _abort_task(task_id: str, api_key: str) -> None:
    """Best-effort cancellation of a task that must not run to completion."""
    try:
        _upstream("DELETE", f"/api/v1/tasks/{task_id}", api_key=api_key, timeout=30)
    except Exception:  # noqa: BLE001 - the attempt is already refused
        return


def check_ffmpeg() -> str:
    """Confirm the FFmpeg binary the pinned pipeline actually calls."""
    try:
        from app.utils import utils
        binary = utils.get_ffmpeg_binary()
        completed = subprocess.run([binary, "-version"], capture_output=True, timeout=30)
    except Exception as error:  # noqa: BLE001 - any failure must fail closed
        raise SelfTestFailure(
            REASON_FFMPEG_UNAVAILABLE,
            f"pinned pipeline FFmpeg probe failed: {type(error).__name__}",
        ) from error

    if completed.returncode != 0:
        raise SelfTestFailure(
            REASON_FFMPEG_UNAVAILABLE,
            f"pinned pipeline FFmpeg exited with {completed.returncode}",
        )
    return binary


def _write_silence(path: Path, seconds: float = 1.0, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


#: The approved narration of the submit-route probe is a tone, not silence and not speech.
#: Silence could not tell an approved voice from a missing one, and a TTS fallback would
#: still produce "some audio", so the rendered track is measured against this frequency:
#: a substitution is audible in the spectrum, not only in the volume (§9.6).
PROBE_VOICE_FREQUENCY = 440.0
#: Loudness is normalised by the pinned compose (``loudnorm``), so only the pitch of the
#: approved narration is compared; the level is only required to be audible at all.
PROBE_VOICE_TONE_AMPLITUDE = 12000
PROBE_VOICE_FREQUENCY_TOLERANCE = 0.10
PROBE_VOICE_MINIMUM_RMS = 0.02
#: Full-scale of the PCM s16 narration the gate reads, so the RMS is a plain fraction.
_SAMPLE_FULL_SCALE = 32768.0


def _write_voice_tone(path: Path, seconds: float = 1.0, rate: int = 24000,
                      frequency: float = PROBE_VOICE_FREQUENCY) -> None:
    """Write the approved narration: lossless PCM s16, exactly as §9.11.4 requires."""
    frames = int(rate * seconds)
    samples = bytearray()
    for index in range(frames):
        value = int(PROBE_VOICE_TONE_AMPLITUDE
                    * math.sin(2 * math.pi * frequency * index / rate))
        samples += value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(samples))


#: Only the head of the narration is measured: the pinned compose normalises the level, so
#: the pitch has to be read from a single steady stretch of the track, not from its tail.
SPECTRUM_WINDOW = 8192


def _pcm_window(samples: bytes) -> list[float]:
    """Decode little-endian PCM s16 into the window the spectrum is measured over."""
    usable = len(samples) - len(samples) % 2
    if usable <= 0:
        return []
    frames = array.array("h")
    frames.frombytes(samples[:usable])
    if sys.byteorder != "little":
        frames.byteswap()
    return [float(value) for value in frames[:SPECTRUM_WINDOW]]


def _magnitude_spectrum(window: list[float], size: int) -> list[float]:
    """Magnitude spectrum of a zero-padded window, computed without a numeric stack.

    The wrapper runs inside the pinned runtime, where NumPy is present, but this check must
    also hold where the measurement is reproduced and where the gate is exercised as a unit:
    a refusal that only works with an optional dependency is not a refusal.
    """
    values = [complex(value, 0.0) for value in window]
    values.extend([0j] * (size - len(values)))
    # Iterative radix-2 Cooley-Tucker, in place.
    mirrored = 0
    for index in range(1, size):
        bit = size >> 1
        while mirrored & bit:
            mirrored ^= bit
            bit >>= 1
        mirrored |= bit
        if index < mirrored:
            values[index], values[mirrored] = values[mirrored], values[index]
    length = 2
    while length <= size:
        angle = -2 * math.pi / length
        step = complex(math.cos(angle), math.sin(angle))
        for start in range(0, size, length):
            factor = 1 + 0j
            half = length >> 1
            for offset in range(start, start + half):
                even = values[offset]
                odd = values[offset + half] * factor
                values[offset] = even + odd
                values[offset + half] = even - odd
                factor *= step
        length <<= 1
    return [abs(value) for value in values[: size // 2 + 1]]


def _frequency_and_rms(samples: bytes, rate: int) -> tuple[float, float]:
    """Dominant frequency and RMS of raw PCM s16 samples, as the render is decoded.

    The pinned compose normalises the loudness of the narration, so the pitch is what
    identifies the approved voice; the level is only used to prove the track is audible.
    """
    window = _pcm_window(samples)
    if not window:
        return 0.0, 0.0
    total = sum(value * value for value in window)
    rms = math.sqrt(total / len(window)) / _SAMPLE_FULL_SCALE
    size = 1 << max(0, len(window) - 1).bit_length()
    spectrum = _magnitude_spectrum(window, size)
    peak = max(range(len(spectrum)), key=spectrum.__getitem__)
    return peak * rate / size, rms


def _pinned_ffmpeg_binary() -> str:
    """The FFmpeg binary the pinned pipeline itself calls, for decoding the render."""
    try:
        from app.utils import utils

        return utils.get_ffmpeg_binary()
    except Exception as error:  # noqa: BLE001 - any failure must fail closed
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"pinned FFmpeg binary is not resolvable: {type(error).__name__}",
        ) from error


def _rendered_voice_profile(path: Path, binary: str, rate: int = 16000) -> dict:
    """Decode the rendered narration and measure it, instead of trusting the log."""
    completed = subprocess.run(
        [binary, "-nostdin", "-loglevel", "error", "-i", str(path),
         "-vn", "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True, timeout=120,
    )
    if completed.returncode != 0:
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"rendered narration could not be decoded (ffmpeg exited "
            f"{completed.returncode})",
        )
    frequency, rms = _frequency_and_rms(completed.stdout, rate)
    return {"frequency_hz": frequency, "rms": rms}


def _verify_rendered_voice(profile: dict) -> None:
    """Refuse a render whose narration is not the approved voice (§9.6)."""
    frequency = float(profile.get("frequency_hz") or 0.0)
    rms = float(profile.get("rms") or 0.0)
    if rms < PROBE_VOICE_MINIMUM_RMS:
        raise SelfTestFailure(
            REASON_VOICE_STAGING_FAILED,
            f"rendered narration is inaudible (rms={rms:.4f}), so the approved voice "
            f"cannot have been used",
        )
    drift = abs(frequency - PROBE_VOICE_FREQUENCY) / PROBE_VOICE_FREQUENCY
    if drift > PROBE_VOICE_FREQUENCY_TOLERANCE:
        raise SelfTestFailure(
            REASON_VOICE_STAGING_FAILED,
            f"rendered narration is not the approved voice: dominant "
            f"{frequency:.0f}Hz against the approved {PROBE_VOICE_FREQUENCY:.0f}Hz",
        )


#: The pinned runtime refuses a local material whose edge is smaller than 480px (with a
#: 10px tolerance) and fails the whole task at the ``materials`` stage with
#: ``no valid local video materials were found``. A probe smaller than that proves
#: nothing about the route: it is rejected before anything downstream runs.
PINNED_MIN_MATERIAL_EDGE = 480
PINNED_MATERIAL_EDGE_TOLERANCE = 10

#: Portrait 9:16 probe geometry, matching the aspect the factory submits. Both edges
#: stay at or above the pinned minimum, so the pinned preprocessor accepts the material.
PROBE_CLIP_WIDTH = 486
PROBE_CLIP_HEIGHT = 864


def _stage_probe_clip(path: Path, seconds: int = 3, colour: str = "black") -> None:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"color=c={colour}:s={PROBE_CLIP_WIDTH}x{PROBE_CLIP_HEIGHT}:r=30:d={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )


def _verify_probe_geometry(path: Path) -> tuple[int, int]:
    """Refuse a probe the pinned preprocessor would reject, before submitting it.

    The pinned runtime validates local materials itself and fails the whole task at the
    ``materials`` stage when an edge is below its minimum. A self-test that uploaded such
    a probe proved nothing about the route it was meant to prove, so the geometry is
    checked here where the failure can still name its own cause (§9.12).
    """
    from moviepy import VideoFileClip

    with VideoFileClip(str(path)) as probe:
        width, height = (int(value) for value in probe.size)
    minimum = PINNED_MIN_MATERIAL_EDGE - PINNED_MATERIAL_EDGE_TOLERANCE
    if width < minimum or height < minimum:
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"probe clip {width}x{height} is below the pinned minimum material edge of "
            f"{minimum}px, which the pinned runtime refuses at the materials stage",
        )
    return width, height


#: One probe per approved scene: label, distinct colour and its exact approved duration
#: (§9.3 rows 2–4). The durations differ on purpose — a compose path that ignored them,
#: or reordered the scenes, cannot produce this timeline.
PROBE_SCENES = (
    ("scene-1", "red", 2),
    ("scene-2", "green", 3),
    ("scene-3", "blue", 4),
)


def _mean_colour(clip, at_seconds: float) -> tuple[float, float, float]:
    import numpy as np

    frame = clip.get_frame(min(max(at_seconds, 0.0), float(clip.duration) - 0.1))
    return tuple(float(value) for value in np.asarray(frame, dtype=float).reshape(-1, 3).mean(axis=0))


def _submit_route_payload(material_key: str, marker: str, seconds: float) -> dict:
    """The fixed §9.4 submit set, built exactly as the bridge builds it.

    Every field that could start an external generation, a narration synthesis or a
    publication is pinned here, so the self-test submits the same request the factory
    submits and not a friendlier variant of it.
    """
    return {
        "video_subject": "Vertep runtime self-test",
        "video_script": "Approved self-test narration.",
        "video_terms": None,
        "video_source": "local",
        "video_materials": [{"provider": "local", "url": material_key, "duration": seconds}],
        "custom_audio_file": marker,
        "video_aspect": "9:16",
        "video_fit_mode": "contain",
        "video_concat_mode": "sequential",
        "video_transition_mode": None,
        "video_clip_duration": max(1, int(math.ceil(seconds))),
        "video_clip_speed": 1.0,
        "match_materials_to_script": False,
        "video_count": 1,
        "bgm_type": "",
        "bgm_file": "",
        "bgm_volume": 0.0,
        "subtitle_enabled": False,
        "video_language": "",
        "n_threads": 2,
    }


def _self_test_client():
    """A synchronous client that drives this wrapper's own ASGI app.

    ``httpx.ASGITransport`` is asynchronous: the pinned lock ships ``httpx==0.28.1``,
    where a synchronous ``httpx.Client`` cannot enter it at all
    (``AttributeError: 'ASGITransport' object has no attribute '__enter__'``) and could
    not serve an ``async def`` route anyway. Starlette's test client runs the very same
    ASGI application the container serves, so the self-test still proves the real
    upload/submit routes instead of calling the handlers directly.
    """
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="http://wrapper")


def check_submit_route(*, deadline_seconds: float = 600.0,
                       poll_seconds: float = 3.0) -> dict:
    """Render one scene through the real submit → status → download route (P3/P4).

    :func:`check_media_pipeline` proves the pinned compose helper in isolation, which
    is not enough: the attempt the factory makes goes through the wrapper's own
    multipart upload, submit invariants, durable submit record, voice staging, pinned
    status mapping and download. This check drives exactly those routes in-process and
    then verifies the downloaded bytes, so a runtime that would narrate or publish on
    its own, reorder scenes or ignore the staged voice cannot pass §9.12.

    It also closes the only remaining way the voice could be missing at the audio
    stage: the submit route proves the approved bytes before it contacts the upstream,
    the accepted task directory is verified again once the upstream has named it, and
    the narration of the finished render is decoded and measured against the approved
    tone. A fallback to the upstream's own TTS therefore fails this check instead of
    producing a Job that was narrated by something nobody approved (§9.6).

    The default deadline is deliberately shorter than the budget the runtime check grants
    this endpoint: a render that never finishes has to be reported here, with the gate
    that was running, instead of surfacing as a socket timeout on the caller.
    """
    try:
        from moviepy import VideoFileClip
    except ImportError as error:
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"pinned media decoder is not importable: {error}",
        ) from error

    label, colour, seconds = "route-1", "red", 2
    staging = Path(tempfile.mkdtemp(prefix="vertep-submit-self-test-"))
    submit_key = f"self-test-{int(time.time())}-{os.getpid()}"
    task_id: str | None = None
    try:
        clip_path = staging / f"{label}.mp4"
        _stage_probe_clip(clip_path, seconds=seconds, colour=colour)
        _verify_probe_geometry(clip_path)
        with VideoFileClip(str(clip_path)) as source:
            expected = _mean_colour(source, seconds / 2)
        voice_path = staging / "voice.wav"
        _write_voice_tone(voice_path, seconds=seconds)
        marker = _store_staged_voice(voice_path.name, voice_path.read_bytes())["voice"]

        with _self_test_client() as client:
            upload = client.post(
                "/api/v1/video_materials",
                headers={"x-vertep-filename": clip_path.name},
                content=clip_path.read_bytes(),
            )
            if upload.status_code != 200:
                raise SelfTestFailure(
                    REASON_MEDIA_PIPELINE_FAILED,
                    f"scene upload was refused: HTTP {upload.status_code} {upload.text[:200]}",
                )
            stored = (upload.json().get("data") or {}).get("file")
            if not isinstance(stored, str) or not stored.strip():
                raise SelfTestFailure(
                    REASON_SCHEMA_UNSUPPORTED,
                    "pinned upload returned no storage key",
                )
            submitted = client.post(
                "/api/v1/videos",
                headers={"x-vertep-submit-key": submit_key},
                content=json.dumps(_submit_route_payload(stored, marker, seconds)).encode(),
            )
        if submitted.status_code != 200:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                f"submit was refused: HTTP {submitted.status_code} {submitted.text[:300]}",
            )
        task_id = ((submitted.json().get("data") or {}).get("task_id")
                   or submitted.json().get("data", {}).get("task_id"))
        if not task_id:
            raise SelfTestFailure(
                REASON_SCHEMA_UNSUPPORTED, "submit returned no data.task_id")

        record = _submit_record(submit_key) or {}
        if record.get("state") != "staged":
            raise SelfTestFailure(
                REASON_VOICE_STAGING_FAILED,
                f"approved voice is not staged for the accepted task (state={record.get('state')!r})",
            )

        final_path: str | None = None
        last_state: object = None
        with httpx.Client() as client:
            deadline = time.monotonic() + deadline_seconds
            while True:
                status = client.get(f"{_upstream_url()}/api/v1/tasks/{task_id}",
                                    headers=headers(read_api_key()), timeout=30)
                status.raise_for_status()
                data = status.json().get("data") or {}
                last_state = data.get("state")
                if last_state == 1:
                    videos = data.get("videos") or []
                    if videos:
                        final_path = str(videos[0]).lstrip("/")
                    break
                if last_state == -1:
                    raise SelfTestFailure(
                        REASON_MEDIA_PIPELINE_FAILED,
                        f"pinned render failed at {data.get('failed_stage')!r}: "
                        f"{data.get('error')}",
                    )
                if time.monotonic() >= deadline:
                    raise SelfTestFailure(
                        REASON_MEDIA_PIPELINE_FAILED,
                        f"pinned render did not finish within {deadline_seconds:.0f}s",
                    )
                time.sleep(poll_seconds)
            if not final_path:
                raise SelfTestFailure(
                    REASON_MEDIA_PIPELINE_FAILED, "completed render reported no output file")
            download = client.get(f"{_upstream_url()}/api/v1/download/{final_path}",
                                  headers=headers(read_api_key()), timeout=600)
        data_bytes = download.content
        if download.status_code != 200 or not data_bytes:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                f"download returned HTTP {download.status_code} with {len(data_bytes)} bytes",
            )

        rendered = staging / "rendered.mp4"
        rendered.write_bytes(data_bytes)
        with VideoFileClip(str(rendered)) as probe:
            duration = float(probe.duration or 0.0)
            width, height = probe.size
            actual = _mean_colour(probe, min(seconds / 2, max(duration - 0.2, 0.1)))
        drift = max(abs(a - b) for a, b in zip(actual, expected))
        if duration <= 0 or int(width) <= 0 or int(height) <= 0:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED, "downloaded render has no usable video stream")
        if drift > 48:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                f"downloaded render does not show the uploaded scene: expected {expected}, "
                f"rendered {actual}",
            )
        # The narration is measured, not assumed: a TTS fallback, a missing track or an
        # ignored staged voice all produce audio, but not the approved one (§9.6).
        voice_profile = _rendered_voice_profile(rendered, _pinned_ffmpeg_binary())
        _verify_rendered_voice(voice_profile)
        return {
            "submit_key": submit_key,
            "task_id": task_id,
            "state": last_state,
            "bytes": len(data_bytes),
            "duration_seconds": round(duration, 3),
            "width": int(width),
            "height": int(height),
            "mean_colour": [round(value, 1) for value in actual],
            "expected_colour": [round(value, 1) for value in expected],
            "voice_staged": True,
            "voice_frequency_hz": round(voice_profile["frequency_hz"], 1),
            "voice_rms": round(voice_profile["rms"], 4),
            "output": final_path,
        }
    finally:
        if task_id:
            try:
                _upstream("DELETE", f"/api/v1/tasks/{task_id}", api_key=read_api_key(),
                          timeout=30)
            except Exception:  # noqa: BLE001 - the self-test must not fail on cleanup
                pass
        shutil.rmtree(staging, ignore_errors=True)


def check_media_pipeline() -> dict:
    """Prove the pinned compose path honours scene order, scene durations and aspect.

    This is the §9.12 media check plus the §9.3 rows the runtime is actually responsible
    for: the materials arrive as pre-cut clips in scene order, each one carries the exact
    approved scene duration, and the compose uses the job aspect with ``contain`` fit —
    the same call the bridge makes. Each probe clip has its own solid colour, so the
    timeline of the rendered output is verifiable instead of merely "playable": a compose
    that reordered or re-timed the scenes changes the colours at the expected timestamps.

    Nothing here calls a paid or external provider: the voice is a locally written WAV and
    the upstream's own TTS/LLM/publication are never involved.
    """
    try:
        from app.models.schema import VideoAspect, VideoConcatMode, VideoFitMode
        from app.services.video import combine_videos
        from moviepy import VideoFileClip
    except ImportError as error:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            f"pinned media pipeline is not importable: {error}",
        ) from error

    staging = Path(tempfile.mkdtemp(prefix="vertep-self-test-"))
    try:
        scenes = []
        for label, colour, seconds in PROBE_SCENES:
            clip_path = staging / f"{label}.mp4"
            _stage_probe_clip(clip_path, seconds=seconds, colour=colour)
            _verify_probe_geometry(clip_path)
            scenes.append({"label": label, "path": clip_path, "seconds": seconds,
                           "colour": colour})
        total_seconds = sum(scene["seconds"] for scene in scenes)
        audio_path = staging / "probe.wav"
        _write_silence(audio_path, seconds=total_seconds)
        combined_path = staging / "probe-combined.mp4"
        _combine_probe_scenes(
            combine_videos, combined_path, scenes, audio_path, VideoAspect, VideoConcatMode,
            VideoFitMode,
        )

        if not combined_path.is_file() or combined_path.stat().st_size == 0:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                "pinned combine_videos produced no output",
            )

        expected_colours = {}
        with VideoFileClip(str(combined_path)) as probe:
            duration = float(probe.duration or 0.0)
            width, height = probe.size
            timeline = _probe_scene_timeline(probe, scenes, VideoFileClip, expected_colours)
        if duration <= 0 or int(width) <= 0 or int(height) <= 0:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                "pinned combine_videos output has no usable video stream",
            )
        _verify_probe_timeline(timeline, total_seconds, width, height, VideoAspect)
        return {
            "duration_seconds": round(duration, 3),
            "width": int(width),
            "height": int(height),
            "bytes": combined_path.stat().st_size,
            "scenes": timeline,
            "scene_order": [entry["label"] for entry in timeline],
            "expected_scene_order": [scene["label"] for scene in scenes],
            "expected_duration_seconds": total_seconds,
            "aspect": str(getattr(VideoAspect.portrait, "value", VideoAspect.portrait)),
            "fit_mode": str(getattr(VideoFitMode.contain, "value", VideoFitMode.contain)),
            "concat_mode": str(getattr(VideoConcatMode.sequential, "value",
                                        VideoConcatMode.sequential)),
        }
    finally:
        for child in staging.glob("*"):
            child.unlink(missing_ok=True)
        staging.rmdir()


def _combine_probe_scenes(combine_videos, combined_path: Path, scenes: list[dict],
                          audio_path: Path, VideoAspect, VideoConcatMode, VideoFitMode) -> None:
    """Compose the probe scenes through the pinned entry point with the bridge's values."""
    try:
        combine_videos(
            str(combined_path),
            [str(scene["path"]) for scene in scenes],
            str(audio_path),
            video_aspect=VideoAspect.portrait,
            video_concat_mode=VideoConcatMode.sequential,
            video_transition_mode=None,
            max_clip_duration=max(scene["seconds"] for scene in scenes),
            threads=2,
            clip_speed=1.0,
            video_fit_mode=VideoFitMode.contain,
        )
    except Exception as error:  # noqa: BLE001 - any failure must fail closed
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"pinned combine_videos failed: {type(error).__name__}: {error}",
        ) from error


def _probe_scene_timeline(probe, scenes: list[dict], VideoFileClip,
                          expected_colours: dict) -> list[dict]:
    """Sample the rendered output at the timestamp each approved scene must occupy."""
    offset = 0.0
    timeline = []
    for scene in scenes:
        midpoint = offset + scene["seconds"] / 2
        with VideoFileClip(str(scene["path"])) as source:
            expected = _mean_colour(source, scene["seconds"] / 2)
        expected_colours[scene["label"]] = expected
        timeline.append({
            "label": scene["label"],
            "seconds": scene["seconds"],
            "at_seconds": round(midpoint, 3),
            "mean_colour": [round(value, 1) for value in _mean_colour(probe, midpoint)],
            "expected_colour": [round(value, 1) for value in expected],
        })
        offset += scene["seconds"]
    return timeline


def _verify_probe_timeline(timeline: list[dict], total_seconds: float, width: int,
                           height: int, VideoAspect) -> None:
    """Fail closed unless the output shows every scene, in order, at its own duration."""
    if len(timeline) != len(PROBE_SCENES):
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"compose produced {len(timeline)} of {len(PROBE_SCENES)} scenes",
        )
    # Each probe is a distinct solid colour, so a mean difference beyond this tolerance
    # means the output shows a different scene (or none) at that timestamp.
    for entry in timeline:
        drift = max(abs(actual - expected) for actual, expected
                    in zip(entry["mean_colour"], entry["expected_colour"]))
        if drift > 48:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                f"scene order or duration is wrong at {entry['at_seconds']}s: "
                f"expected {entry['expected_colour']}, rendered {entry['mean_colour']}",
            )
    expected_width, expected_height = VideoAspect.portrait.to_resolution()
    if (int(width), int(height)) != (int(expected_width), int(expected_height)):
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"compose ignored the job aspect: rendered {width}x{height}, "
            f"expected {expected_width}x{expected_height}",
        )
    last = timeline[-1]["at_seconds"]
    if last >= total_seconds:
        raise SelfTestFailure(
            REASON_MEDIA_PIPELINE_FAILED,
            f"scene timeline runs past the approved duration: {last}s of {total_seconds}s",
        )


# ---------------------------------------------------------------------------
# ASGI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Vertep MoneyPrinterTurbo wrapper",
    version=WRAPPER_API_VERSION,
    docs_url=None,
    redoc_url=None,
)


@app.exception_handler(SelfTestFailure)
async def _self_test_failure(request: Request, failure: SelfTestFailure) -> JSONResponse:
    """Report a fail-closed refusal with its stable reason code on every route.

    A durable record that cannot be trusted or a voice that cannot be staged has to
    reach the caller as a reason, not as an opaque 500 that invites a blind retry.
    """
    return JSONResponse(
        status_code=503,
        content={"status": 503, "message": failure.message, "reason": failure.code},
    )


def _contract() -> tuple[str, str, str]:
    """Read the pinned upstream reference and bridge versions from the contract.

    The pin lives in ``adapters.providers.runtime_manifest`` rather than in
    ``video_engines``: the image copies only ``adapters`` and ``services``, and
    importing the engine module would drag in ``publishers`` and the platform
    adapters that this container deliberately does not carry.
    """
    from adapters.providers.base import BRIDGE_SCHEMA_VERSION
    from adapters.providers.runtime_manifest import PINNED_UPSTREAM_COMMIT

    return PINNED_UPSTREAM_COMMIT, BRIDGE_SCHEMA_VERSION, BRIDGE_SCHEMA_VERSION


@app.get("/health")
def health() -> JSONResponse:
    """Cheap readiness: wrapper up, inventory readable, upstream reachable."""
    checks: dict[str, object] = {}
    status_code = 200
    try:
        api_key = read_api_key()
        commit, bridge_version, bridge_schema_version = _contract()
        checks["snapshot"] = verify_runtime_snapshot(
            commit, bridge_version, bridge_schema_version
        )
        # Issue #122 §9.12: readiness proves endpoint, auth and schema on the real
        # executor. A 200 alone is not enough — an unauthenticated API, or one whose
        # submit schema drifted, must never be reported as ready.
        with httpx.Client() as client:
            check_upstream_authenticated(client, api_key)
        checks["upstream_auth_enforced"] = True
        checks["submit_schema"] = sorted(check_submit_schema())
        checks["required_submit_fields"] = sorted(FIXED_SUBMIT_FIELDS)
        checks["voice_staging"] = task_local_voice_capability()
    except SelfTestFailure as failure:
        status_code = 503
        checks["reason"] = failure.code
        checks["detail"] = failure.message
    except httpx.HTTPError as error:
        status_code = 503
        checks["reason"] = REASON_UPSTREAM_UNREACHABLE
        checks["detail"] = f"{type(error).__name__}: {error}"

    return JSONResponse(
        status_code=status_code,
        content={"service": SERVICE_NAME, "status": "ready" if status_code == 200 else "unavailable",
                 "checks": checks},
    )


@app.get("/runtime")
def runtime() -> JSONResponse:
    """Report the pinned runtime snapshot this container was built from."""
    try:
        commit, bridge_version, bridge_schema_version = _contract()
        snapshot = verify_runtime_snapshot(commit, bridge_version, bridge_schema_version)
    except SelfTestFailure as failure:
        return JSONResponse(
            status_code=503,
            content={"service": SERVICE_NAME, "reason": failure.code,
                     "detail": failure.message},
        )
    return JSONResponse(
        status_code=200,
        content={"service": SERVICE_NAME, **snapshot,
                 "capabilities": task_local_voice_capability()},
    )


@app.get("/sbom")
def sbom() -> JSONResponse:
    """Publish the SBOM of this runtime image (Issue #122 §9.15).

    The document is generated from the inventory that was just verified, so it
    describes the code and dependencies that are actually running, and it is
    content-addressed: the caller can recompute the digest and compare it with
    the digest recorded in the release manifest.
    """
    try:
        commit, bridge_version, bridge_schema_version = _contract()
        verify_runtime_snapshot(commit, bridge_version, bridge_schema_version)
        document = generate_sbom(load_manifest(_inventory_path()))
    except SelfTestFailure as failure:
        return JSONResponse(
            status_code=503,
            content={"service": SERVICE_NAME, "reason": failure.code,
                     "detail": failure.message},
        )
    except RuntimeManifestError as error:
        return JSONResponse(
            status_code=503,
            content={"service": SERVICE_NAME, "reason": REASON_INVENTORY_UNVERIFIED,
                     "detail": str(error)},
        )
    return JSONResponse(status_code=200, content=document)


# ---------------------------------------------------------------------------
# Pinned upstream proxy
#
# The upstream API binds loopback inside this container, so the wrapper is the
# only externally reachable endpoint (Issue #122 §1.3). Every call is gated on a
# verified snapshot and the runtime API key: an unverified runtime must never
# receive production work, and it must never be reachable without a key.
# ---------------------------------------------------------------------------


def _gate() -> str:
    """Return the runtime API key after proving the runtime is dispatchable."""
    api_key = read_api_key()
    commit, bridge_version, bridge_schema_version = _contract()
    verify_runtime_snapshot(commit, bridge_version, bridge_schema_version)
    return api_key


def _upstream(method: str, path: str, *, api_key: str, **kwargs) -> httpx.Response:
    try:
        with httpx.Client(follow_redirects=False) as client:
            return client.request(
                method, f"{_upstream_url()}{path}",
                headers={**headers(api_key), **kwargs.pop("headers", {})},
                **kwargs,
            )
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{REASON_UPSTREAM_UNREACHABLE}: {type(error).__name__}",
        ) from error


def _passthrough(response: httpx.Response) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


def _complete_recorded_staging(record: dict, *, api_key: str) -> None:
    """Finish staging the approved voice of an already accepted upstream task.

    The wrapper writes the task mapping and the staged voice as two separate steps, so
    a restart between them used to leave a task that could never be narrated by the
    approved audio while the record already claimed success. Staging is
    content-addressed and idempotent, so the repeat submit repairs the attempt before it
    is answered as accepted — and aborts the upstream task when it cannot (Issue #122
    P3/P6, §9.6).
    """
    submit_key = str(record.get("submit_key") or "")
    task_id = str(record.get("task_id") or "")
    marker = str(record.get("voice_marker") or "")
    if record.get("state") == "staged":
        return
    if not task_id or not marker:
        raise SelfTestFailure(
            REASON_SUBMIT_RECORD_UNREADABLE,
            "submit record has neither a task id nor a staged voice to complete",
        )
    try:
        _stage_task_voice(task_id, marker)
    except SelfTestFailure as failure:
        _abort_task(task_id, api_key)
        _remember_submit(submit_key, task_id=None, marker=marker, state="refused")
        raise SelfTestFailure(
            REASON_VOICE_STAGING_FAILED,
            f"approved voice could not be staged for {task_id}: {failure.message}",
        ) from failure
    _remember_submit(submit_key, task_id=task_id, marker=marker, state="staged")


@app.post("/api/v1/videos")
async def proxy_submit(request: Request) -> Response:
    """Submit one task and stage the approved voice into its task directory.

    The approved narration never leaves Vertep as a filesystem path: CORE stages
    the bytes through ``POST /api/v1/voice`` and submits a content-addressed
    marker. This handler turns that marker into the task-local file the pinned
    resolver requires, and refuses the whole attempt if it cannot prove the file
    is readable there (Issue #122 §9.3 row 5, §9.6).
    """
    api_key = _gate()
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="submit body must be JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="submit body must be an object")

    marker = _require_submit_invariants(payload)
    submit_key = request.headers.get("x-vertep-submit-key", "").strip()
    recorded = _submit_record(submit_key) if submit_key else None
    if recorded:
        if recorded.get("state") == "refused":
            return JSONResponse(
                status_code=409,
                content={
                    "status": 409,
                    "message": "this submit key was already refused and must not be reused",
                    "reason": REASON_VOICE_STAGING_FAILED,
                },
            )
        if recorded.get("task_id"):
            # The same attempt is retried after a lost response: the upstream work
            # already exists, so it is answered from the durable record instead of
            # being created twice (Issue #122 §5, §9.8). Staging is completed first,
            # because the record can outlive the process that staged the voice.
            _complete_recorded_staging(recorded, api_key=api_key)
            return JSONResponse(
                status_code=200,
                content={"status": 200, "message": "reconciled", "data": {"task_id": recorded["task_id"]}},
            )
        if recorded.get("state") == "submitting":
            # A first attempt with this key is still in flight, so forwarding a
            # second POST could create a second upstream task for one render. The
            # repeat is refused and the caller must reconcile the key instead of
            # resubmitting blindly.
            return JSONResponse(
                status_code=409,
                content={
                    "status": 409,
                    "message": "submit is already in flight for this key",
                    "reason": REASON_SUBMIT_UNKNOWN,
                },
            )
    try:
        # The approved audio can only be placed in the task directory once the upstream
        # has named the task, but its bytes can be proved here. Proving them first closes
        # the one window in which a submit could create a render that has no approved
        # narration: nothing is sent upstream unless the voice is really staged
        # (Issue #122 P3, §9.6).
        _verified_staged_voice(marker)
    except SelfTestFailure as failure:
        return JSONResponse(
            status_code=503,
            content={"status": 503, "message": failure.message,
                     "reason": REASON_VOICE_STAGING_FAILED, "cause": failure.code},
        )
    _remember_submit(submit_key, task_id=None, marker=marker)
    try:
        response = _upstream(
            "POST", "/api/v1/videos", api_key=api_key,
            content=json.dumps({
                **payload,
                "custom_audio_file": f"{VOICE_TASK_FILENAME}{marker_suffix(marker)}",
            }).encode("utf-8"),
            headers={"content-type": "application/json"},
            timeout=120,
        )
    except httpx.HTTPError:
        # The upstream call itself failed: the key stays recorded without a task,
        # so a later reconciliation reports UNKNOWN instead of a false success.
        raise
    if response.status_code >= 400:
        if response.status_code < 500:
            # A 4xx is a definitive refusal: the upstream created no task, so the key
            # describes an attempt that never happened and must stay usable. Leaving it
            # reserved would turn one rejected submit into a permanently blocked attempt.
            _forget_submit(submit_key)
        # A 5xx keeps the key without a task: the upstream may have accepted the work
        # before it failed to answer, so a later reconciliation reports UNKNOWN rather
        # than a false success (Issue #122 §5).
        return _passthrough(response)

    task_id = _submitted_task_id(response)
    _remember_submit(submit_key, task_id=task_id, marker=marker, state="submitted")
    try:
        _stage_task_voice(task_id, marker)
    except SelfTestFailure as failure:
        # The task exists but cannot be narrated by the approved voice. Leave no
        # accepted work behind: the attempt is cancelled, the key is marked refused
        # and CORE is told why.
        _abort_task(task_id, api_key)
        _remember_submit(submit_key, task_id=None, marker=marker, state="refused")
        return JSONResponse(
            status_code=503,
            content={"status": 503, "message": failure.message,
                     "reason": REASON_VOICE_STAGING_FAILED, "cause": failure.code},
        )
    _remember_submit(submit_key, task_id=task_id, marker=marker, state="staged")
    return _passthrough(response)


@app.get("/api/v1/videos/{submit_key}")
async def submit_status(submit_key: str) -> Response:
    """Reconcile a lost submit response through the durable submit key (§5).

    ``submitted`` means the upstream work exists and can be polled; ``unknown``
    means the runtime cannot prove it, so CORE must not treat the attempt as
    accepted and must not resubmit blindly. ``voice_staged`` reports whether the
    approved audio is already verified inside the pinned task directory, so a caller
    can tell "accepted" from "accepted but not yet narrated by the approved voice".

    ``runtime_state`` is the real state of the upstream process, because a logical
    cancel proves nothing about compute: a busy task answers ``409`` to DELETE and
    keeps rendering until it observes the abort (§9.8). It is resolved from the
    upstream task rather than assumed, and an unreachable upstream is reported as
    ``unreachable`` so a caller can never read silence as "stopped".
    """
    _gate()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", submit_key):
        raise HTTPException(status_code=400, detail="invalid submit key")
    record = _submit_record(submit_key)
    if not record:
        return JSONResponse(status_code=404, content={"status": 404, "state": "unknown"})
    task_id = record.get("task_id")
    record_state = str(record.get("state") or "")
    return JSONResponse(
        status_code=200,
        content={
            "status": 200,
            # ``state`` stays the reconciliation contract the engine reads: only the
            # presence of an upstream task decides "submitted" vs "unknown".
            "state": "submitted" if task_id else "unknown",
            "record_state": record_state,
            "voice_staged": record_state == "staged",
            "runtime_state": _runtime_state(task_id),
            "data": {"task_id": task_id} if task_id else {},
        },
    )


#: Upstream task states of the pinned runtime: 1 finished, 4 running, -1 failed.
_RUNTIME_STATES = {1: "finished", -1: "failed", 4: "running"}


def _runtime_state(task_id: str | None) -> str:
    """Real upstream state of one task: ``running``/``finished``/``failed``.

    ``absent`` means the upstream no longer knows the task, so nothing of it can be
    holding compute. ``unreachable`` means exactly that — the state is unknown, never
    that the process stopped.
    """
    if not task_id:
        return "absent"
    try:
        response = _upstream("GET", f"/api/v1/tasks/{task_id}", api_key=_gate(), timeout=30)
    except httpx.HTTPError:
        return "unreachable"
    if response.status_code == 404:
        return "absent"
    if response.status_code >= 400:
        return "unreachable"
    try:
        payload = response.json()
    except ValueError:
        return "unreachable"
    data = payload.get("data") if isinstance(payload, dict) else None
    raw = data.get("state") if isinstance(data, dict) else None
    try:
        state = int(raw)
    except (TypeError, ValueError):
        return "unreachable"
    return _RUNTIME_STATES.get(state, "unreachable")


def _store_staged_voice(name: str, payload: bytes) -> dict:
    """Store approved voice bytes content-addressed and return the staging marker.

    Shared by the staging route and the submit-route self-test, so both prove the
    same contract: the marker is the SHA256 of the bytes, and the same bytes always
    land on the same path (Issue #122 §9.3 row 5).
    """
    suffix = Path(name).suffix.lower()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
        or ".." in name
        or suffix not in ALLOWED_VOICE_SUFFIXES
    ):
        raise HTTPException(status_code=400, detail="invalid voice file name")
    if not payload:
        raise HTTPException(status_code=400, detail="empty voice file")
    if len(payload) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="voice file is too large")

    digest = hashlib.sha256(payload).hexdigest()
    target = _voice_dir() / f"{digest}{suffix}"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(payload)
        temporary.replace(target)
    return {
        "voice": f"{VOICE_MARKER_PREFIX}{digest}{suffix}",
        "sha256": digest,
        "bytes": len(payload),
    }


@app.post(VOICE_STAGING_ROUTE)
async def stage_approved_voice(request: Request) -> Response:
    """Stage the approved voice bytes for a later submit (Issue #122 §9.3 row 5)."""
    _gate()
    name = request.headers.get("x-vertep-filename", "").strip()
    staged = _store_staged_voice(name, await request.body())
    return JSONResponse(
        status_code=200,
        content={"status": 200, "message": "success", "data": staged},
    )


@app.post("/api/v1/video_materials")
async def proxy_material_upload(request: Request) -> Response:
    """Forward one scene clip as the multipart body the pinned API expects.

    The engine already holds the bytes and the file name, so the wrapper builds
    the multipart envelope itself and never has to parse one.
    """
    api_key = _gate()
    filename = request.headers.get("x-vertep-filename", "").strip()
    # The name is written into a multipart header, so anything that could break
    # out of the quoted filename (quotes, CR/LF, path separators) is refused
    # instead of sanitised: the runtime must receive exactly the staged name.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid scene clip name")
    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="empty scene clip")
    boundary = "vertep-material-boundary"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    response = _upstream("POST", "/api/v1/video_materials", api_key=api_key,
                         content=body,
                         headers={"content-type": f"multipart/form-data; boundary={boundary}"},
                         timeout=300)
    return _passthrough(response)


@app.delete("/api/v1/videos/{submit_key}")
def abort_submitted_task(submit_key: str) -> Response:
    """Abort the attempt of a durable submit key (§5).

    Cancellation is addressed by the submit key, not by a transient upstream id,
    because the key is the identity CORE owns for the whole attempt and survives a
    lost response. A still running upstream task answers 409 and is reported as
    still running, never as cancelled; an unknown key is 404.
    """
    api_key = _gate()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", submit_key):
        raise HTTPException(status_code=400, detail="invalid submit key")
    record = _submit_record(submit_key)
    task_id = (record or {}).get("task_id")
    if not task_id:
        return JSONResponse(status_code=404, content={"status": 404, "state": "unknown"})
    return _passthrough(
        _upstream("DELETE", f"/api/v1/tasks/{task_id}", api_key=api_key, timeout=30)
    )


@app.get("/api/v1/tasks/{task_id}")
def proxy_task_status(task_id: str) -> Response:
    api_key = _gate()
    return _passthrough(
        _upstream("GET", f"/api/v1/tasks/{task_id}", api_key=api_key, timeout=30)
    )


@app.delete("/api/v1/tasks/{task_id}")
def proxy_task_delete(task_id: str) -> Response:
    api_key = _gate()
    return _passthrough(
        _upstream("DELETE", f"/api/v1/tasks/{task_id}", api_key=api_key, timeout=30)
    )


@app.get("/api/v1/download/{file_path:path}")
def proxy_download(file_path: str) -> Response:
    api_key = _gate()
    return _passthrough(
        _upstream("GET", f"/api/v1/download/{file_path}", api_key=api_key, timeout=600)
    )


@app.get("/self-test")
def self_test() -> JSONResponse:
    """Run the full Issue #122 §9.12 eligibility gate."""
    checks: dict[str, object] = {}
    try:
        api_key = read_api_key()
        commit, bridge_version, bridge_schema_version = _contract()
        checks["snapshot"] = verify_runtime_snapshot(
            commit, bridge_version, bridge_schema_version
        )
        with httpx.Client() as client:
            check_upstream_authenticated(client, api_key)
        checks["upstream"] = {"authenticated": True, "url": _upstream_url()}
        checks["submit_schema"] = check_submit_schema()
        checks["ffmpeg"] = {"binary": check_ffmpeg()}
        checks["media_pipeline"] = check_media_pipeline()
        checks["submit_route"] = check_submit_route()
        checks["voice_staging"] = task_local_voice_capability()
    except SelfTestFailure as failure:
        return JSONResponse(
            status_code=503,
            content={"service": SERVICE_NAME, "status": "failed",
                     "reason": failure.code, "detail": failure.message,
                     "checks": checks},
        )
    except Exception as error:  # noqa: BLE001 - unexpected failures still fail closed
        return JSONResponse(
            status_code=503,
            content={"service": SERVICE_NAME, "status": "failed",
                     "reason": REASON_MEDIA_PIPELINE_FAILED,
                     "detail": f"{type(error).__name__}: {error}",
                     "checks": checks},
        )

    return JSONResponse(
        status_code=200,
        content={"service": SERVICE_NAME, "status": "passed", "checks": checks},
    )


if __name__ == "__main__":  # pragma: no cover - container entrypoint uses uvicorn
    print(json.dumps({"service": SERVICE_NAME, "python": sys.version.split()[0],
                      "inventory": str(_inventory_path()),
                      "digest": hashlib.sha256(b"vertep-moneyprinter").hexdigest()},
                     sort_keys=True))