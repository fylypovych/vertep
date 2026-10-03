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

import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
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
    REASON_UPSTREAM_UNAUTHENTICATED,
    REASON_UPSTREAM_UNREACHABLE,
    REASON_VOICE_STAGING_UNSUPPORTED,
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

# Reason codes and REQUIRED_SUBMIT_FIELDS come from adapters.providers.base so the
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
    """Confirm the pinned upstream still declares the fields we submit."""
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

    missing = [name for name in REQUIRED_SUBMIT_FIELDS if name not in fields]
    if missing:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            f"upstream TaskVideoRequest is missing required fields: {', '.join(missing)}",
        )
    return {
        name: _annotation_name(fields[name].annotation)
        for name in REQUIRED_SUBMIT_FIELDS
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
    path = _submit_record_path(submit_key)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _remember_submit(submit_key: str, *, task_id: str | None) -> None:
    if not submit_key:
        return
    path = _submit_record_path(submit_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"submit_key": submit_key, "task_id": task_id,
               "state": "submitted" if task_id else "submitting"}
    temporary = path.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


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


def _stage_task_voice(task_id: str, marker: str) -> Path:
    """Place the approved voice inside the pinned task directory."""
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


def _stage_probe_clip(path: Path, seconds: int = 3) -> None:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=320x568:r=30:d={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )


def check_media_pipeline() -> dict:
    """Stage one local clip and prove the pinned compose path returns playable output.

    This is the §9.12 media check: it exercises the real MoviePy/FFmpeg assembly
    the engine depends on, without calling any paid or external provider.
    """
    try:
        from app.models.schema import VideoAspect, VideoConcatMode
        from app.services.video import combine_videos
        from moviepy import VideoFileClip
    except ImportError as error:
        raise SelfTestFailure(
            REASON_SCHEMA_UNSUPPORTED,
            f"pinned media pipeline is not importable: {error}",
        ) from error

    staging = Path(tempfile.mkdtemp(prefix="vertep-self-test-"))
    try:
        clip_path = staging / "probe.mp4"
        audio_path = staging / "probe.wav"
        combined_path = staging / "probe-combined.mp4"
        _stage_probe_clip(clip_path)
        _write_silence(audio_path)

        try:
            combine_videos(
                str(combined_path),
                [str(clip_path)],
                str(audio_path),
                video_aspect=VideoAspect.portrait,
                video_concat_mode=VideoConcatMode.sequential,
                video_transition_mode=None,
                max_clip_duration=5,
                threads=2,
                clip_speed=1.0,
            )
        except Exception as error:  # noqa: BLE001 - any failure must fail closed
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                f"pinned combine_videos failed: {type(error).__name__}: {error}",
            ) from error

        if not combined_path.is_file() or combined_path.stat().st_size == 0:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                "pinned combine_videos produced no output",
            )

        try:
            with VideoFileClip(str(combined_path)) as probe:
                duration = float(probe.duration or 0.0)
                width, height = probe.size
        except Exception as error:  # noqa: BLE001 - decode failure must fail closed
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                f"pinned combine_videos output is not decodable: {type(error).__name__}",
            ) from error

        if duration <= 0 or int(width) <= 0 or int(height) <= 0:
            raise SelfTestFailure(
                REASON_MEDIA_PIPELINE_FAILED,
                "pinned combine_videos output has no usable video stream",
            )
        return {
            "duration_seconds": round(duration, 3),
            "width": int(width),
            "height": int(height),
            "bytes": combined_path.stat().st_size,
        }
    finally:
        for child in staging.glob("*"):
            child.unlink(missing_ok=True)
        staging.rmdir()


# ---------------------------------------------------------------------------
# ASGI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Vertep MoneyPrinterTurbo wrapper",
    version=WRAPPER_API_VERSION,
    docs_url=None,
    redoc_url=None,
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
        checks["required_submit_fields"] = list(REQUIRED_SUBMIT_FIELDS)
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
        if recorded.get("task_id"):
            # The same attempt is retried after a lost response: the upstream work
            # already exists, so it is answered from the durable record instead of
            # being created twice (Issue #122 §5, §9.8).
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
                    "reason": "upstream_submit_unknown",
                },
            )
    _remember_submit(submit_key, task_id=None)
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
        return _passthrough(response)

    task_id = _submitted_task_id(response)
    _remember_submit(submit_key, task_id=task_id)
    try:
        _stage_task_voice(task_id, marker)
    except SelfTestFailure as failure:
        # The task exists but cannot be narrated by the approved voice. Leave no
        # accepted work behind: the attempt is cancelled and CORE is told why.
        _abort_task(task_id, api_key)
        return JSONResponse(
            status_code=503,
            content={"status": 503, "message": failure.message, "reason": failure.code},
        )
    return _passthrough(response)


@app.get("/api/v1/videos/{submit_key}")
async def submit_status(submit_key: str) -> Response:
    """Reconcile a lost submit response through the durable submit key (§5).

    ``submitted`` means the upstream work exists and can be polled; ``unknown``
    means the runtime cannot prove it, so CORE must not treat the attempt as
    accepted and must not resubmit blindly.
    """
    _gate()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", submit_key):
        raise HTTPException(status_code=400, detail="invalid submit key")
    record = _submit_record(submit_key)
    if not record:
        return JSONResponse(status_code=404, content={"status": 404, "state": "unknown"})
    task_id = record.get("task_id")
    return JSONResponse(
        status_code=200,
        content={
            "status": 200,
            "state": "submitted" if task_id else "unknown",
            "data": {"task_id": task_id} if task_id else {},
        },
    )


@app.post(VOICE_STAGING_ROUTE)
async def stage_approved_voice(request: Request) -> Response:
    """Stage the approved voice bytes for a later submit (Issue #122 §9.3 row 5)."""
    _gate()
    name = request.headers.get("x-vertep-filename", "").strip()
    suffix = Path(name).suffix.lower()
    # The name becomes a file inside the pinned task directory, so any name that
    # could escape it or break a header is refused instead of sanitised.
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
        or ".." in name
        or suffix not in ALLOWED_VOICE_SUFFIXES
    ):
        raise HTTPException(status_code=400, detail="invalid voice file name")
    payload = await request.body()
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
    return JSONResponse(
        status_code=200,
        content={
            "status": 200,
            "message": "success",
            "data": {
                "voice": f"{VOICE_MARKER_PREFIX}{digest}{suffix}",
                "sha256": digest,
                "bytes": len(payload),
            },
        },
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