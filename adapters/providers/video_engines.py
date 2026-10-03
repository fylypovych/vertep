"""Video engine backends behind the ``VideoEngine`` interface.

Phase 5 of the open-source audit formalizes the FFmpeg assembly pipeline as a
high-level ``VideoEngine`` driver and makes external engines (MoneyPrinter /
ShortGPT style, both MIT references) pluggable without Vertep giving up the Job
lifecycle or character metadata.

* ``NativeVertepEngine`` — the default, wraps the current FFmpeg assembly
  pipeline (via ``AssemblyProvider``). No external service required.
* ``MoneyPrinterEngine`` / ``ShortGPTEngine`` — opt-in HTTP-backed engines that
  submit the asset spec to a remote render endpoint, poll for status and
  download the rendered video. Vertep never owns the external render logic
  (REUSE, not copy). They talk to ``httpx`` only through an injectable transport
  so tests drive the exact flow with ``FakeTransport``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import struct
import tempfile
import secrets
import shutil
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ._http import check_response as _check
from .base import (
    BRIDGE_SCHEMA_VERSION,
    REASON_INVENTORY_UNVERIFIED,
    REASON_SCHEMA_UNSUPPORTED,
    REASON_SNAPSHOT_MISMATCH,
    REASON_UPSTREAM_UNAUTHENTICATED,
    REASON_VOICE_STAGING_UNSUPPORTED,
    REASON_SUBMIT_UNKNOWN,
    REQUIRED_SUBMIT_FIELDS,
    BridgeContractError,
    VideoEngine,
)
from .runtime_manifest import (
    INVENTORY_FORMAT,
    PINNED_UPSTREAM_REFERENCE,
    RuntimeManifestError,
    verify_inventory,
)
from publishers.transport import HttpTransport


@dataclass(frozen=True)
class BridgeContractProfile:
    """Declarative description of what a pinned external engine can honour.

    Issue #122 P1: every mandatory Job input is classified as supported,
    bridge-adapted or unsupported here, so validation before dispatch is derived
    from the declaration instead of from assumptions about the upstream API.
    """

    name: str
    upstream_reference: str
    endpoints: dict[str, str]
    status_states: dict[int, str]
    supported_aspect_ratios: tuple[str, ...]
    supported_task_types: tuple[str, ...]
    image_suffixes: tuple[str, ...]
    video_suffixes: tuple[str, ...]
    max_scene_duration_seconds: float
    requires_narration_audio: bool
    submission_parameters: tuple[str, ...]
    post_step_components: tuple[str, ...]
    fixed_submission_values: dict[str, object] = field(default_factory=dict)
    pre_dispatch_rejections: tuple[str, ...] = ()
    min_image_edge: int = 0
    duration_tolerance_seconds: float = 0.0

    def supported_presets(self) -> tuple[str, ...]:
        from adapters.ffmpeg import FFmpegAdapter

        return tuple(sorted(FFmpegAdapter.PRESETS))


MONEY_PRINTER_CONTRACT = BridgeContractProfile(
    name="moneyprinter_v1",
    upstream_reference=PINNED_UPSTREAM_REFERENCE,
    endpoints={
        "submit": "/api/v1/videos",
        "status": "/api/v1/tasks/{task_id}",
        "download": "/api/v1/download/{file_path}",
        "upload_material": "/api/v1/video_materials",
        # Issue #122 P6: durable submit key reconciliation. The wrapper records the
        # key before it contacts the upstream, so a repeat submit is idempotent and
        # a lost response can be resolved into a real task id or an explicit
        # UNKNOWN state.
        "submit_status": "/api/v1/videos/{submit_key}",
        "voice": "/api/v1/voice",
        # Cancellation is addressed by the durable submit key, not by a transient
        # upstream id, so it survives a lost submit response (§5).
        "cancel": "/api/v1/videos/{submit_key}",
    },
    status_states={4: "processing", 1: "complete", -1: "failed"},
    supported_aspect_ratios=("16:9", "9:16"),
    supported_task_types=("image", "video"),
    image_suffixes=(".jpg", ".jpeg", ".png", ".bmp"),
    video_suffixes=(".mp4", ".mov", ".mkv", ".webm"),
    max_scene_duration_seconds=15.0,
    requires_narration_audio=True,
    submission_parameters=(
        "video_subject",
        "video_script",
        "video_source",
        "video_materials",
        "custom_audio_file",
        "video_aspect",
        "video_fit_mode",
        "video_concat_mode",
        "video_transition_mode",
        "video_clip_duration",
        "video_clip_speed",
        "match_materials_to_script",
        "video_count",
        "bgm_type",
        "subtitle_enabled",
    ),
    post_step_components=(
        "audio_mix",
        "subtitle_burn",
        "watermark_overlay",
        "preset_geometry",
    ),
    fixed_submission_values={
        "video_source": "local",
        "video_fit_mode": "contain",
        "video_concat_mode": "sequential",
        "video_transition_mode": None,
        "video_clip_speed": 1.0,
        "match_materials_to_script": False,
        "video_count": 1,
        "bgm_type": "",
        "subtitle_enabled": False,
    },
    pre_dispatch_rejections=(
        "schema_version_unsupported",
        "unsupported_task_type",
        "no_materials",
        "material_channel_mismatch",
        "asset_unreadable",
        "asset_below_min_resolution",
        "unsupported_asset_format",
        "clip_unreadable",
        "duration_count_mismatch",
        "invalid_duration",
        "duration_mismatch",
        "duration_exceeds_clip_limit",
        "missing_voice_audio",
        "voice_audio_unusable",
        "music_unusable",
        "subtitle_unusable",
        "unsupported_aspect",
        "preset_unknown",
        "watermark_unusable",
        "output_not_writable",
    ),
    min_image_edge=470,
    duration_tolerance_seconds=1.0,
)


class NativeVertepEngine(VideoEngine):
    """Default engine: current FFmpeg assembly pipeline.

    For ``task_type == "video"`` it concatenates video clips via
    :meth:`assemble_clips`; otherwise it assembles still images into a motion
    video with zoom/fades/audio via :meth:`assemble`.
    """

    def __init__(self, assembly=None) -> None:
        from adapters.providers import DefaultAssemblyProvider
        from adapters.ffmpeg import FFmpegAdapter

        self._assembly = assembly or DefaultAssemblyProvider(FFmpegAdapter())

    @property
    def provider(self) -> str:
        return "native"

    def render(
        self,
        output: Path,
        *,
        images: list[Path] | None = None,
        clips: list[Path] | None = None,
        durations: list[float] | None = None,
        audio: Path | None = None,
        music: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
        watermark: Path | None = None,
        task_type: str = "image",
        script: str | None = None,
        submit_key: str | None = None,
    ) -> Path:
        # The Native route renders Vertep's own assets; the approved narration is
        # only consumed by an external engine (Issue #122 §9.4). ``submit_key`` is
        # meaningless for a local render, which never creates upstream work.
        output.parent.mkdir(parents=True, exist_ok=True)
        if task_type == "video":
            self._assembly.assemble_clips(
                output,
                clips=list(clips or []),
                audio=audio,
                subtitles=subtitles,
                aspect_ratio=aspect_ratio,
                preset=preset,
            )
        else:
            self._assembly.assemble(
                output,
                images=list(images or []),
                durations=durations,
                audio=audio,
                music=music,
                subtitles=subtitles,
                aspect_ratio=aspect_ratio,
                preset=preset,
                watermark=watermark,
            )
        return output
def _path(p) -> str | None:
    return str(p) if p else None


class _JsonResponse:
    """Minimal response stand-in for a reconciled submit (§9.8).

    Reconciliation answers from the runtime's durable submit record, not from a
    live upstream body, but the caller expects the same envelope it would get from
    a real submit.
    """

    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _paths(paths) -> list[str]:
    return [str(p) for p in (paths or [])]


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """Read pixel dimensions from the file header without extra dependencies."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(65536)
    except OSError:
        return None
    if header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
        return struct.unpack(">II", header[16:24])
    if header[:2] == b"BM" and len(header) >= 26:
        width, height = struct.unpack("<ii", header[18:26])
        return (abs(width), abs(height))
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        chunk = header[12:16]
        if chunk == b"VP8X":
            return (
                int.from_bytes(header[24:27], "little") + 1,
                int.from_bytes(header[27:30], "little") + 1,
            )
        return None
    if header[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(header):
            if header[offset] != 0xFF:
                offset += 1
                continue
            marker = header[offset + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                height, width = struct.unpack(">HH", header[offset + 5:offset + 9])
                return (int(width), int(height))
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            offset += 2 + int.from_bytes(header[offset + 2:offset + 4], "big")
        return None
    if header[:2] in {b"P6", b"P3"}:
        parts = header[2:64].split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (int(parts[0]), int(parts[1]))
    return None


def _container_readable(path: Path, suffix: str) -> bool:
    """Verify the file really carries a container header of the declared kind."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    if suffix in {".mp4", ".mov"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if suffix in {".mkv", ".webm"}:
        return header[:4] == b"\x1a\x45\xdf\xa3"
    return False


def _wav_duration(path: Path) -> float | None:
    """Duration of a PCM WAV file, or ``None`` when it is not a readable WAV."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (OSError, wave.Error):
        return None
    if not rate or frames <= 0:
        return None
    return frames / float(rate)


class RemoteVideoEngine(VideoEngine):
    """Base for HTTP-backed external video engines.

    Subclasses only set ``name``, ``env_url`` and ``env_token``; the render flow
    (submit → poll → download) is shared. Any external engine that can expose
    ``POST /render``, ``GET /jobs/<id>`` and ``GET /download/<id>`` can be
    bridged behind ``VideoEngine`` without adding code to Vertep's core.
    """

    name = "remote"
    env_url = "REMOTE_VIDEO_ENGINE_URL"
    env_token = "REMOTE_VIDEO_ENGINE_TOKEN"
    contract_profile: BridgeContractProfile | None = None

    def __init__(
            self,
            url: str | None = None,
            token: str | None = None,
            transport=None,
            poll_interval: float = 2.0,
            timeout: float = 600.0,
            assembly=None,
        ) -> None:
            self._url = (
                (url if url is not None else os.getenv(self.env_url, "")) or ""
            ).rstrip("/")
            self._token = (
                token
                if token is not None
                else os.getenv(self.env_token, "")
            )
            self._transport = transport or HttpTransport()
            self._poll_interval = poll_interval
            self._timeout = timeout
            self._assembly_impl = assembly
            self.current_job_id: str | None = None

    # Issue #122 §9.3/§9.5: the executor pre-cut and the post-step finish are
    # control-plane assembly, so an external engine finishes the way Native
    # does. The FFmpeg assembly is resolved lazily so that health checks and
    # pre-dispatch validation never require a local FFmpeg binary.
    @property
    def _assembly(self):
        if self._assembly_impl is None:
            from adapters.ffmpeg import FFmpegAdapter
            from adapters.providers import DefaultAssemblyProvider

            self._assembly_impl = DefaultAssemblyProvider(FFmpegAdapter())
        return self._assembly_impl

    @_assembly.setter
    def _assembly(self, provider) -> None:
        self._assembly_impl = provider

    @property
    def provider(self) -> str:
        return self.name

    def configured(self) -> bool:
        return bool(self._url)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self._token:
            headers.setdefault("Authorization", f"Bearer {self._token}")
        return headers

    def _submit_task(self, request: dict, *, submit_key: str | None):
        """Submit once, and reconcile a lost response instead of resubmitting.

        §5/§9.8: the pinned upstream has no idempotency key, so a response lost
        after the upstream accepted the work must never be retried blindly. The
        durable submit key makes the submit idempotent in the runtime: the wrapper
        records the key before it contacts the upstream and answers a repeat with
        the task it already created. Without a key the submit is sent once and a
        lost response is terminal, never ambiguous.
        """
        headers = self._headers({"x-vertep-submit-key": submit_key} if submit_key else None)
        try:
            submit = self._transport.post(
                f"{self._url}/api/v1/videos", headers=headers, json=request, timeout=120,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            reconciled = self._reconcile_submit(submit_key)
            if reconciled is None:
                raise BridgeContractError(
                    REASON_SUBMIT_UNKNOWN,
                    f"{self.name} lost the submit response and cannot prove whether "
                    f"the work was accepted: {error}",
                ) from error
            return reconciled
        if submit.status_code == 409 and submit_key:
            # The runtime reports that an attempt with this key is still in flight:
            # resolving it into the existing task is the only correct answer,
            # because forwarding the submit again could create a second upstream
            # task for one render.
            reconciled = self._reconcile_submit(submit_key)
            if reconciled is None:
                raise BridgeContractError(
                    REASON_SUBMIT_UNKNOWN,
                    f"{self.name} reports an in-flight submit for key {submit_key!r} "
                    f"that cannot be resolved into a task yet",
                )
            return reconciled
        _check(submit)
        return submit

    def _reconcile_submit(self, submit_key: str | None):
        """Ask the runtime which upstream task a durable submit key produced."""
        if not submit_key:
            return None
        profile = self.contract_profile
        path = profile.endpoints.get("submit_status", "/api/v1/videos/{submit_key}")
        try:
            response = self._transport.get(
                f"{self._url}{path.format(submit_key=submit_key)}",
                headers=self._headers(), timeout=60,
            )
            if response.status_code != 200:
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict) or body.get("state") != "submitted":
            return None
        data = body.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not task_id:
            return None
        return _JsonResponse({"status": 200, "message": "success", "data": {"task_id": task_id}})

    def _asset_spec(
        self,
        *,
        images, clips, durations, audio, music, subtitles,
        aspect_ratio, preset, watermark, task_type,
        schema_version: str = BRIDGE_SCHEMA_VERSION,
    ) -> dict:
        if schema_version != BRIDGE_SCHEMA_VERSION:
            raise BridgeContractError(
                "schema_version_unsupported",
                f"Unsupported bridge schema version: {schema_version}",
            )
        if aspect_ratio not in {"16:9", "9:16", "1:1"}:
            raise BridgeContractError(
                "unsupported_aspect",
                f"Invalid or unsupported aspect ratio: {aspect_ratio}",
            )
        return {
            "schema_version": schema_version,
            "task_type": task_type,
            "aspect_ratio": aspect_ratio,
            "preset": preset,
            "images": _paths(images),
            "clips": _paths(clips),
            "durations": list(durations or []),
            "audio": _path(audio),
            "music": _path(music),
            "subtitles": _path(subtitles),
            "watermark": _path(watermark),
        }

    def capabilities(self) -> dict:
        """Describe what this engine can honour (Issue #122 P1).

        Callable on an instance; the previous declaration was an unbound
        function, so any instance call raised ``TypeError``. Engines without a
        pinned upstream profile keep the generic contract.

        Includes eligibility/readiness checks per Issue #122 P2.
        """
        profile = self.contract_profile
        if profile is None:
            return {
                "schema_version": BRIDGE_SCHEMA_VERSION,
                "contract_profile": "generic",
                "upstream_reference": None,
                "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
                "supported_task_types": ["image", "video"],
                "requires_narration_audio": False,
                "post_step_components": [],
                "pre_dispatch_rejections": ["schema_version_unsupported", "unsupported_aspect"],
                "ready": self.configured(),
            }
        base_caps = {
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "contract_profile": profile.name,
            "upstream_reference": profile.upstream_reference,
            "endpoints": dict(profile.endpoints),
            "status_mapping": {
                str(state): name for state, name in sorted(profile.status_states.items())
            },
            "supported_aspect_ratios": list(profile.supported_aspect_ratios),
            "supported_task_types": list(profile.supported_task_types),
            "supported_presets": list(profile.supported_presets()),
            "image_suffixes": list(profile.image_suffixes),
            "video_suffixes": list(profile.video_suffixes),
            "max_scene_duration_seconds": profile.max_scene_duration_seconds,
            "min_image_edge": profile.min_image_edge,
            "duration_tolerance_seconds": profile.duration_tolerance_seconds,
            "requires_narration_audio": profile.requires_narration_audio,
            "submission_parameters": list(profile.submission_parameters),
            "fixed_submission_values": dict(profile.fixed_submission_values),
            "post_step_components": list(profile.post_step_components),
            "pre_dispatch_rejections": list(profile.pre_dispatch_rejections),
            "ready": self.configured(),
        }

        # Add health check for MoneyPrinterEngine per P2
        if isinstance(self, MoneyPrinterEngine):
            health = self.health_check()
            base_caps["health"] = health
            base_caps["ready"] = health["available"]

        return base_caps

    def validate_contract(
        self,
        *,
        images, clips, durations, audio, music, subtitles,
        aspect_ratio, preset, watermark, task_type,
        output: Path | str | None = None,
        schema_version: str = BRIDGE_SCHEMA_VERSION,
        script: str | None = None,
    ) -> dict:
        """Validate Job inputs against the declared profile before dispatch.

        Returns the normalised render plan (materials, narration and the
        executor post-step requirements). Raises ``BridgeContractError`` with a
        stable ``code`` for every rejected input; never degrades to another
        engine and never ignores an input silently.
        """
        profile = self.contract_profile
        if schema_version != BRIDGE_SCHEMA_VERSION:
            raise BridgeContractError(
                "schema_version_unsupported",
                f"Unsupported bridge schema version: {schema_version}",
            )
        if profile is None:
            self._asset_spec(
                images=images, clips=clips, durations=durations, audio=audio,
                music=music, subtitles=subtitles, aspect_ratio=aspect_ratio,
                preset=preset, watermark=watermark, task_type=task_type,
                schema_version=schema_version,
            )
            return {
                "materials": [Path(item) for item in _paths(images or clips)],
                "narration": Path(audio) if audio else None,
                "post_step": {},
            }

        if task_type not in profile.supported_task_types:
            raise BridgeContractError(
                "unsupported_task_type",
                f"Unsupported task type for {profile.name}: {task_type}",
            )
        if aspect_ratio not in profile.supported_aspect_ratios:
            raise BridgeContractError(
                "unsupported_aspect",
                f"Invalid or unsupported aspect ratio: {aspect_ratio}",
            )
        if preset is not None and preset not in profile.supported_presets():
            raise BridgeContractError(
                "preset_unknown",
                f"Unknown output preset: {preset}",
            )

        image_list = [Path(item) for item in (images or [])]
        clip_list = [Path(item) for item in (clips or [])]
        if task_type == "image" and clip_list:
            raise BridgeContractError(
                "material_channel_mismatch",
                "task_type=image must not receive video clips",
            )
        if task_type == "video" and image_list:
            raise BridgeContractError(
                "material_channel_mismatch",
                "task_type=video must not receive scene images",
            )
        materials = image_list if task_type == "image" else clip_list
        if not materials:
            raise BridgeContractError(
                "no_materials",
                "At least one approved scene asset is required",
            )

        suffixes = (
            profile.image_suffixes if task_type == "image" else profile.video_suffixes
        )
        for material in materials:
            if not material.is_file():
                raise BridgeContractError(
                    "asset_unreadable",
                    f"Scene asset is not a readable file: {material.name}",
                )
            if material.suffix.lower() not in suffixes:
                raise BridgeContractError(
                    "unsupported_asset_format",
                    f"Unsupported scene asset format for {profile.name}: {material.suffix or material.name}",
                )
            if task_type == "video":
                if not _container_readable(material, material.suffix.lower()):
                    raise BridgeContractError(
                        "clip_unreadable",
                        f"Scene clip is not a readable {material.suffix} container: {material.name}",
                    )
                continue
            if profile.min_image_edge:
                dimensions = _image_dimensions(material)
                if dimensions is None:
                    raise BridgeContractError(
                        "asset_unreadable",
                        f"Scene image dimensions are unreadable: {material.name}",
                    )
                if min(dimensions) < profile.min_image_edge:
                    raise BridgeContractError(
                        "asset_below_min_resolution",
                        f"Scene image is smaller than the required "
                        f"{profile.min_image_edge}px edge: {material.name} {dimensions[0]}x{dimensions[1]}",
                    )

        duration_list = list(durations or [])
        if len(duration_list) != len(materials):
            raise BridgeContractError(
                "duration_count_mismatch",
                f"Expected {len(materials)} scene durations, got {len(duration_list)}",
            )
        for duration in duration_list:
            try:
                value = float(duration)
            except (TypeError, ValueError):
                raise BridgeContractError(
                    "invalid_duration",
                    f"Scene duration is not a number: {duration!r}",
                ) from None
            if not math.isfinite(value) or value <= 0:
                raise BridgeContractError(
                    "invalid_duration",
                    f"Scene duration must be finite and positive: {duration!r}",
                )
            if value > profile.max_scene_duration_seconds:
                raise BridgeContractError(
                    "duration_exceeds_clip_limit",
                    f"Scene duration exceeds {profile.max_scene_duration_seconds:g}s upstream limit: {value:g}",
                )

        if profile.requires_narration_audio and audio is None:
            raise BridgeContractError(
                "missing_voice_audio",
                f"{profile.name} requires the approved narration audio",
            )
        narration = Path(audio) if audio else None
        if narration is not None and not narration.is_file():
            raise BridgeContractError(
                "voice_audio_unusable",
                f"Narration audio is not a readable file: {narration.name}",
            )
        if profile.duration_tolerance_seconds and narration is not None:
            voice_duration = (
                _wav_duration(narration)
                if narration.suffix.lower() == ".wav"
                else None
            )
            if voice_duration is not None:
                drift = abs(sum(float(value) for value in duration_list) - voice_duration)
                if drift > profile.duration_tolerance_seconds:
                    raise BridgeContractError(
                        "duration_mismatch",
                        f"Approved scene durations drift {drift:.2f}s from the "
                        f"{voice_duration:.2f}s narration (tolerance "
                        f"{profile.duration_tolerance_seconds:g}s)",
                    )
        if music is not None and not Path(music).is_file():
            raise BridgeContractError(
                "music_unusable",
                f"Music file is not readable: {Path(music).name}",
            )
        if subtitles is not None:
            subtitle_path = Path(subtitles)
            if not subtitle_path.is_file() or subtitle_path.suffix.lower() != ".srt":
                raise BridgeContractError(
                    "subtitle_unusable",
                    f"Approved subtitles must be a readable .srt file: {subtitle_path.name}",
                )
        if watermark is not None and not Path(watermark).is_file():
            raise BridgeContractError(
                "watermark_unusable",
                f"Watermark file is not readable: {Path(watermark).name}",
            )
        if output is not None:
            self._require_writable_output(output)

        return {
            "task_type": task_type,
            "aspect_ratio": aspect_ratio,
            "preset": preset,
            "materials": materials,
            "durations": [float(value) for value in duration_list],
            "narration": narration,
            "post_step": self._post_step_plan(
                narration=narration,
                music=Path(music) if music else None,
                subtitles=Path(subtitles) if subtitles else None,
                watermark=Path(watermark) if watermark else None,
                preset=preset,
                task_type=task_type,
            ),
        }

    @staticmethod
    def _require_writable_output(output: Path | str) -> Path:
        """Reject an output destination that cannot receive the imported video."""
        target = Path(output)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise BridgeContractError(
                "output_not_writable",
                f"Output directory cannot be created: {target.parent} ({err})",
            ) from err
        if target.exists() and target.is_dir():
            raise BridgeContractError(
                "output_not_writable",
                f"Output destination is a directory: {target}",
            )
        try:
            with tempfile.NamedTemporaryFile(dir=str(target.parent), delete=True):
                pass
        except OSError as err:
            raise BridgeContractError(
                "output_not_writable",
                f"Output directory is not writable: {target.parent} ({err})",
            ) from err
        return target

    def _post_step_plan(
        self, *, narration, music, subtitles, watermark, preset, task_type
    ) -> dict:
        profile = self.contract_profile
        plan: dict[str, object] = {}
        if narration is not None:
            plan["audio_mix"] = {
                "voice": str(narration),
                "music": str(music) if music else None,
                "voice_filter": "loudnorm=I=-16:LRA=11:TP=-1.5",
                "music_volume": 0.15,
                "mix_duration": "first",
            }
        if subtitles is not None:
            plan["subtitle_burn"] = {"source": str(subtitles), "policy": "BURN_SUBTITLES"}
        if watermark is not None and task_type == "image":
            plan["watermark_overlay"] = {
                "source": str(watermark),
                "scale": "max(80, width // 7)",
                "offset": 24,
            }
        if preset is not None:
            plan["preset_geometry"] = {"preset": preset}
        if profile is not None:
            plan["components"] = [
                name for name in profile.post_step_components if name in plan
            ]
        return plan

    def build_upstream_request(
        self,
        *,
        subject: str,
        script: str,
        materials: list[dict],
        narration_path: str,
        aspect_ratio: str,
        clip_duration: int,
    ) -> dict:
        """Map a validated render plan onto the pinned upstream request body."""
        profile = self.contract_profile
        if profile is None:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no pinned upstream request mapping",
            )
        if not script.strip():
            raise BridgeContractError(
                "script_unavailable",
                "Approved script text is required for the upstream submission",
            )
        if aspect_ratio not in profile.supported_aspect_ratios:
            raise BridgeContractError(
                "unsupported_aspect",
                f"Invalid or unsupported aspect ratio: {aspect_ratio}",
            )
        if not isinstance(clip_duration, int) or isinstance(clip_duration, bool):
            raise BridgeContractError(
                "invalid_clip_duration",
                f"Upstream clip duration must be an integer: {clip_duration!r}",
            )
        if not 1 <= clip_duration <= int(profile.max_scene_duration_seconds):
            raise BridgeContractError(
                "invalid_clip_duration",
                f"Upstream clip duration must be within 1..{int(profile.max_scene_duration_seconds)}: {clip_duration}",
            )
        payload = {
            "video_subject": subject,
            "video_script": script,
            "video_terms": None,
            "video_source": "local",
            "video_materials": materials,
            "custom_audio_file": narration_path,
            "video_aspect": aspect_ratio,
            "video_fit_mode": "contain",
            "video_concat_mode": "sequential",
            "video_transition_mode": None,
            "video_clip_duration": clip_duration,
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
        return payload

    def map_upstream_status(self, payload) -> dict:
        """Translate a pinned upstream status body into engine state.

        Only the states declared by the profile are terminal; any other value
        stays ``unknown`` so polling stays bounded instead of guessing success.
        """
        profile = self.contract_profile
        if profile is None:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no pinned upstream status mapping",
            )
        if not isinstance(payload, dict):
            raise BridgeContractError(
                "upstream_status_malformed",
                "Upstream status payload is not an object",
            )
        state = payload.get("state")
        if isinstance(state, bool) or not isinstance(state, int):
            raise BridgeContractError(
                "upstream_status_malformed",
                f"Upstream status payload has no integer state: {state!r}",
            )
        mapped = profile.status_states.get(state, "unknown")
        outputs = payload.get("videos")
        if mapped == "complete":
            if not isinstance(outputs, list) or not outputs or not all(
                isinstance(item, str) and item for item in outputs
            ):
                raise BridgeContractError(
                    "upstream_result_missing_output",
                    "Upstream task completed without a usable output reference",
                )
        return {
            "task_id": payload.get("task_id"),
            "state": mapped,
            "upstream_state": state,
            "progress": payload.get("progress", 0),
            "outputs": list(outputs) if isinstance(outputs, list) else [],
            "failed_stage": payload.get("failed_stage"),
            "error": payload.get("error"),
        }

    @staticmethod
    def parse_output_reference(reference: str) -> str:
        """Normalise an upstream output reference to a task-scoped path.

        Upstream returns either a task-relative URI (``tasks/final-1.mp4``) or the
        same URI behind a configured endpoint. Anything outside the task
        directory is rejected instead of being fetched.
        """
        if not isinstance(reference, str) or not reference.strip():
            raise BridgeContractError(
                "upstream_result_missing_output",
                "Upstream output reference is empty",
            )
        value = reference.strip()
        if value.startswith(("http://", "https://")):
            value = "/" + value.split("://", 1)[1].partition("/")[2]
        parts = [
            part
            for part in posixpath.normpath(value).replace("\\", "/").lstrip("/").split("/")
            if part not in {"", "."}
        ]
        if not parts or ".." in parts or parts[0] != "tasks":
            raise BridgeContractError(
                "upstream_output_path_unsafe",
                f"Upstream output reference is not task-scoped: {reference}",
            )
        return "/".join(parts)

    def _pre_cut_materials(
        self, materials: list[Path], durations: list[float], workdir: Path,
    ) -> list[Path]:
        """Cut every scene asset to exactly its approved duration (§9.3)."""
        cut: list[Path] = []
        for index, (material, duration) in enumerate(zip(materials, durations)):
            target = workdir / f"scene-{index:03d}.mp4"
            try:
                cut.append(self._assembly.pre_cut(material, duration, target))
            except Exception as error:  # noqa: BLE001 - any failure refuses the render
                raise BridgeContractError(
                    "asset_pre_cut_failed",
                    f"Scene {index} could not be cut to {duration:g}s: {error}",
                ) from error
        return cut

    def _get_with_retry(self, path: str, deadline: float, what: str):
        """Read an idempotent runtime resource with bounded retry (§9.4).

        Only reads are retried: a submit or an upload that lost its response may
        already have been accepted upstream and must never be repeated silently.
        """
        url = path if path.startswith("http") else f"{self._url}{path}"
        last_error: Exception | None = None
        for attempt in range(self._max_read_attempts):
            try:
                response = self._transport.get(url, headers=self._headers(), timeout=180)
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = BridgeContractError(
                        "upstream_transient_failure",
                        f"{what} answered {response.status_code}",
                    )
                else:
                    _check(response)
                    return response
            except BridgeContractError:
                raise
            except Exception as error:  # noqa: BLE001 - transport failures are retried
                last_error = error
            if time.monotonic() >= deadline:
                break
            time.sleep(self._read_backoff * (2 ** attempt))
        raise BridgeContractError(
            "upstream_transient_failure",
            f"{what} failed after {self._max_read_attempts} attempts: {last_error}",
        )

    def _verify_readable(self, path: Path) -> int:
        """Prove the final artifact can be read back in full from its location.

        P3 requires an access check on the verified import: a file that exists but
        cannot be read (permissions, a broken path, a partial write on a network
        share) must never be registered as an artifact version.
        """
        try:
            with path.open("rb") as handle:
                digest = hashlib.sha256()
                read = 0
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    read += len(chunk)
                    digest.update(chunk)
        except OSError as error:
            raise BridgeContractError(
                "upstream_result_corrupted",
                f"Imported artifact is not readable: {error}",
            ) from error
        if read != path.stat().st_size:
            raise BridgeContractError(
                "upstream_result_corrupted",
                f"Imported artifact is truncated: read {read} of {path.stat().st_size} bytes",
            )
        return read

    def _verify_media(self, path: Path, *, expected_duration: float | None = None) -> dict:
        """Prove an imported or post-processed file is real, decodable media."""
        try:
            probed = self._assembly.probe(path)
        except Exception as error:  # noqa: BLE001 - decode failure refuses the artifact
            raise BridgeContractError(
                "upstream_result_corrupted",
                f"Downloaded video is not decodable: {error}",
            ) from error
        duration = 0.0
        if isinstance(probed, dict):
            metadata = probed.get("format")
            if isinstance(metadata, dict):
                try:
                    duration = float(metadata.get("duration") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
        tolerance = float(
            getattr(self.contract_profile, "duration_tolerance_seconds", 1.0)
            if self.contract_profile
            else 1.0
        )
        if expected_duration and duration and abs(duration - expected_duration) > (
            tolerance + 1.0
        ):
            raise BridgeContractError(
                "upstream_result_corrupted",
                f"Video duration {duration:.2f}s drifts from the approved "
                f"{expected_duration:.2f}s",
            )
        return {"duration": duration, "bytes": path.stat().st_size}


    def render(
        self,
        output: Path,
        *,
        images: list[Path] | None = None,
        clips: list[Path] | None = None,
        durations: list[float] | None = None,
        audio: Path | None = None,
        music: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
        watermark: Path | None = None,
        task_type: str = "image",
        script: str | None = None,
        submit_key: str | None = None,
    ) -> Path:
        if not self._url:
            raise RuntimeError(
                f"{self.name} is not configured (set {self.env_url})"
            )
        validated = self.validate_contract(
            images=images, clips=clips, durations=durations, audio=audio,
            music=music, subtitles=subtitles, aspect_ratio=aspect_ratio,
            preset=preset, watermark=watermark, task_type=task_type,
            output=output, script=script,
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        # Build upstream request using validated plan
        materials = validated["materials"]
        narration = validated["narration"]
        plan = validated["post_step"]
        duration_total = sum(float(value) for value in (durations or []))

        # For MoneyPrinterEngine, use the new flow with material upload
        if isinstance(self, MoneyPrinterEngine):
            scene_durations = [float(value) for value in (durations or [])]
            if len(scene_durations) != len(materials):
                raise BridgeContractError(
                    "duration_count_mismatch",
                    "Every staged material must keep its approved scene duration",
                )

            # Bridge contract v1 (§9.4): the upstream clip duration is the
            # uniform integer cover of the longest approved scene, capped at the
            # upstream limit. Per-scene exactness is produced by the executor
            # pre-cut, never by inventing a duration here.
            longest_scene = max(scene_durations)
            clip_duration = max(
                1,
                min(
                    int(self.contract_profile.max_scene_duration_seconds),
                    math.ceil(longest_scene),
                ),
            )

            # §9.4: the pinned runtime generates a script when video_script is
            # empty and synthesises its own narration when custom_audio_file is
            # empty. Neither stage is allowed here (§9.6), so both inputs must be
            # real approved values and the render is refused otherwise.
            approved_script = (script or "").strip()
            if not approved_script:
                raise BridgeContractError(
                    "script_unavailable",
                    "Approved narration text is required for an external render",
                )
            if not narration or not Path(narration).is_file():
                raise BridgeContractError(
                    "missing_voice_audio",
                    "Approved voice audio is required for an external render",
                )

            # The runtime must be able to receive the approved voice before a
            # single byte is generated; otherwise its own TTS would run.
            self._require_task_local_voice()

            # §9.3 row 1: the attempt owns its staging area. The name carries a
            # unique attempt token, so two attempts of the same Job version never
            # share staged clips or upstream storage keys, and the ownership is
            # visible both on disk and in the upload cache namespace.
            attempt = f"{output.stem}-{secrets.token_hex(8)}"
            workdir = output.parent / f".{attempt}-bridge"
            workdir.mkdir(parents=True, exist_ok=True)
            try:
                # §9.3 rows 2-3: one clip per scene, cut to exactly the approved
                # scene duration, because the runtime has no per-item duration.
                cut_clips = self._pre_cut_materials(materials, scene_durations, workdir)
                uploaded_materials = self._upload_materials(
                    materials=cut_clips,
                    task_id=attempt,
                    durations=scene_durations,
                )
                staged_voice = self._stage_approved_voice(Path(narration))

                subject = str(output.stem)
                upstream_request = self.build_upstream_request(
                    subject=subject,
                    script=approved_script,
                    materials=uploaded_materials,
                    narration_path=staged_voice,
                    aspect_ratio=aspect_ratio,
                    clip_duration=clip_duration,
                )

                submit = self._submit_task(
                    upstream_request, submit_key=submit_key,
                )
                response_data = submit.json()
                # Unwrap MoneyPrinterTurbo envelope {status, message, data:{...}}
                data = response_data.get("data") if isinstance(response_data, dict) else None
                if isinstance(data, dict):
                    response_data = data
                job_id = response_data.get("task_id")
                if not job_id:
                    raise RuntimeError(f"{self.name} external engine returned no task_id")
                self.current_job_id = job_id

                deadline = time.monotonic() + self._timeout
                status_data = None
                while time.monotonic() < deadline:
                    status_response = self._get_with_retry(
                        f"/api/v1/tasks/{job_id}", deadline, "task status"
                    )
                    status_json = status_response.json()
                    status_payload = status_json.get("data") if isinstance(status_json, dict) and "data" in status_json else status_json
                    status_data = self.map_upstream_status(status_payload)
                    state = status_data.get("state")
                    if state == "complete":
                        break
                    if state == "failed":
                        raise RuntimeError(
                            f"{self.name} external engine failed: "
                            f"{status_data.get('error', 'unknown')}"
                        )
                    time.sleep(self._poll_interval)
                else:
                    raise TimeoutError(f"{self.name} render timed out: {job_id}")

                outputs = status_data.get("outputs", [])
                if not outputs:
                    raise RuntimeError(f"{self.name} external engine returned no outputs")

                # Download the first output
                output_ref = self.parse_output_reference(outputs[0])
                profile = self.contract_profile
                download_endpoint = profile.endpoints.get("download", "/api/v1/download/{file_path}")
                download = self._get_with_retry(
                    download_endpoint.format(file_path=output_ref), deadline, "result download"
                )
            finally:
                self.current_job_id = None
                # The attempt-owned staging area never outlives the attempt, not
                # even when submit, polling or the download failed.
                shutil.rmtree(workdir, ignore_errors=True)
        else:
            # Generic remote engine: use legacy flow without material upload
            spec = self._asset_spec(
                images=images, clips=clips, durations=durations, audio=audio,
                music=music, subtitles=subtitles, aspect_ratio=aspect_ratio,
                preset=preset, watermark=watermark, task_type=task_type,
            )
            submit = self._transport.post(
                f"{self._url}/render",
                headers=self._headers(),
                json=spec,
                timeout=30,
            )
            _check(submit)
            job_id = submit.json().get("job_id")
            if not job_id:
                raise RuntimeError(f"{self.name} external engine returned no job_id")
            self.current_job_id = job_id
            download = None
            try:
                self._wait_for_status(job_id)
                download = self._transport.get(
                    f"{self._url}/download/{job_id}",
                    headers=self._headers(),
                    timeout=180,
                )
                _check(download)
            finally:
                self.current_job_id = None

        if not download:
            raise RuntimeError(f"{self.name} download failed - no response")
        if not download.content:
            raise RuntimeError(f"{self.name} downloaded video content is empty")

        temp_target = output.with_suffix(output.suffix + ".tmp")
        try:
            # Verified output import (§9.5 step 5): the download becomes a file
            # only after its size, container and decodability were proven.
            temp_target.write_bytes(download.content)
            size = temp_target.stat().st_size
            if size == 0:
                raise BridgeContractError(
                    "upstream_result_corrupted",
                    "Imported artifact size verification failed: 0 bytes",
                )
            self._verify_media(temp_target, expected_duration=duration_total)

            # §9.5 steps 1-4: the downloaded render is finished exactly like a
            # Native render before any artifact version is registered.
            staged = output.parent / f".{output.stem}-poststep.mp4"
            try:
                self._assembly.apply_post_step(
                    staged,
                    temp_target,
                    audio=narration,
                    music=music,
                    subtitles=subtitles,
                    aspect_ratio=aspect_ratio,
                    preset=preset,
                    watermark=watermark if task_type == "image" else None,
                )
                self._verify_media(staged, expected_duration=duration_total)
                staged.replace(output)
                # The download copy has served its purpose; the registered
                # artifact is the post-step result, so the temporary file goes.
                temp_target.unlink(missing_ok=True)
            finally:
                staged.unlink(missing_ok=True)

            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("Output verification failed after replace")
            # §9.5 step 5: the registered artifact must be readable in place, not
            # merely present, before its version may be recorded.
            self._verify_readable(output)
            sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
            # Store checksum sidecar for audit
            try:
                (output.with_suffix(output.suffix + ".sha256")).write_text(sha256, encoding="utf-8")
            except Exception:
                pass
            # Cleanup task-scoped staging
            try:
                staging_dir = output.parent / "staging"
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
            except Exception:
                pass
        except Exception:
            temp_target.unlink(missing_ok=True)
            raise
        return output

    def cancel(self, job_id: str | None = None, *, submit_key: str | None = None) -> bool:
        """Cancel an active render job on the remote engine.

        The attempt is addressed by its durable submit key whenever one is known:
        the key is the identity CORE owns for the whole attempt, so cancellation
        works even when the upstream task id never reached CORE (Issue #122 §5).
        """
        if submit_key and self._url:
            return self._abort_by_submit_key(submit_key)
        target_id = job_id or self.current_job_id
        if not target_id or not self._url:
            return False
        try:
            # The pinned upstream exposes task cancellation as
            # DELETE /api/v1/tasks/{task_id}. A busy task answers 409 and
            # an unknown task 404; neither may be reported as a cancelled
            # job (Issue #122 §5).
            resp = self._transport.delete(
                f"{self._url}/api/v1/tasks/{target_id}",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 409:
                return False
            return resp.status_code in {200, 202, 204}
        except Exception:
            return False

    def _abort_by_submit_key(self, submit_key: str) -> bool:
        """Abort an attempt through the durable submit-key abort route."""
        route = None
        if self.contract_profile is not None:
            route = self.contract_profile.endpoints.get("submit_status")
        if not route:
            return False
        try:
            resp = self._transport.delete(
                f"{self._url}{route.format(submit_key=submit_key)}",
                headers=self._headers(),
                timeout=10,
            )
        except Exception:
            return False
        if resp.status_code == 409:
            # Still running upstream: not cancelled, and the caller keeps the
            # attempt fenced logically instead of reporting success.
            return False
        return resp.status_code in {200, 202, 204}

    def _wait_for_status(self, job_id: str) -> dict:
        deadline = time.monotonic() + self._timeout
        consecutive_transient_errors = 0
        while time.monotonic() < deadline:
            try:
                response = self._transport.get(
                    f"{self._url}/api/v1/tasks/{job_id}",
                    headers=self._headers(),
                    timeout=10,
                )
                _check(response)
                status = response.json()
                consecutive_transient_errors = 0
            except Exception as err:
                consecutive_transient_errors += 1
                if consecutive_transient_errors > 5:
                    raise RuntimeError(
                        f"{self.name} failed during polling: {err}"
                    ) from err
                time.sleep(self._poll_interval)
                continue

            # Use map_upstream_status for MoneyPrinterEngine
            if isinstance(self, MoneyPrinterEngine):
                mapped = self.map_upstream_status(status)
                if mapped["state"] == "complete":
                    return status
                if mapped["state"] == "failed":
                    raise RuntimeError(
                        f"{self.name} external engine failed: "
                        f"{mapped.get('error', 'unknown')}"
                    )
            else:
                # Generic remote engine status check
                state = str(status.get("status", "")).upper()
                if state == "READY":
                    return status
                if state in {"FAILED", "ERROR"}:
                    raise RuntimeError(
                        f"{self.name} external engine failed: "
                        f"{status.get('error', 'unknown')}"
                    )
            time.sleep(self._poll_interval)
        raise TimeoutError(f"{self.name} render timed out: {job_id}")


class MoneyPrinterEngine(RemoteVideoEngine):
    """External MoneyPrinterTurbo-style engine (MIT reference) behind VideoEngine."""

    name = "money-printer"
    env_url = "MONEY_PRINTER_URL"
    env_token = "MONEY_PRINTER_TOKEN"
    contract_profile = MONEY_PRINTER_CONTRACT

    def __init__(
        self, *args, max_read_attempts: int = 4, read_backoff: float = 0.5, **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._upload_cache: dict[str, dict] = {}
        # Bounded retry for idempotent reads only (Issue #122 §9.4).
        self._max_read_attempts = max(1, int(max_read_attempts))
        self._read_backoff = max(0.0, float(read_backoff))

    # --- runtime capability gate ---------------------------------------------

    def runtime_snapshot(self) -> dict:
        """Read the verified runtime snapshot from the wrapper."""
        response = self._transport.get(
            f"{self._url}/runtime", headers=self._headers(), timeout=30,
        )
        _check(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                "Runtime snapshot is not an object",
            )
        return payload

    def _require_task_local_voice(self) -> None:
        """Refuse the render unless the runtime can take the approved voice.

        §9.3 row 5 makes the staged voice mandatory and §9.6 forbids the runtime
        from synthesising narration of its own. The wrapper derives the answer
        from the pinned code and from its own staging route, so an incapable
        runtime is refused here — before any material is uploaded or any task is
        created.
        """
        snapshot = self.runtime_snapshot()
        capabilities = snapshot.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                "Runtime does not report its staging capabilities",
            )
        if not capabilities.get("task_local_voice"):
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                capabilities.get("detail")
                or "Runtime cannot receive the approved voice audio",
            )

    def _stage_approved_voice(self, audio: Path) -> str:
        """Upload the approved narration and return its staged reference.

        The runtime stores the bytes itself and can then place them in the task
        directory of the submit that follows; Vertep never sends a filesystem
        path that the runtime could not read (§9.3 row 5).
        """
        payload = Path(audio).read_bytes()
        if not payload:
            raise BridgeContractError(
                "voice_audio_unusable",
                "Approved voice audio is empty",
            )
        response = self._transport.post(
            f"{self._url}{self.contract_profile.endpoints.get('voice', '/api/v1/voice')}",
            headers=self._headers({"x-vertep-filename": Path(audio).name}),
            content=payload,
            timeout=120,
        )
        _check(response)
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        reference = data.get("voice") if isinstance(data, dict) else None
        if not isinstance(reference, str) or not reference.strip():
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                "Runtime did not return a staged voice reference",
            )
        if data.get("sha256") != hashlib.sha256(payload).hexdigest():
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                "Runtime staged a different voice payload than the approved one",
            )
        return reference

    # --- Issue #122 P3/P4 helpers --------------------------------------------


    def _upload_materials(
        self,
        *,
        materials: list[Path],
        task_id: str,
        durations: list[float] | None = None,
    ) -> list[dict]:
        """Upload approved scene assets to the MPT runtime (Issue #122 P3).

        Each asset is uploaded once per (attempt, artifact SHA256) and the
        returned storage key carries the approved scene duration required by
        bridge contract v1 (§9.4). The cache is process-local, so a restart
        re-uploads; durable attempt mapping belongs to P6.

        Raises BridgeContractError on upload failure before submit.
        """
        profile = self.contract_profile
        if profile is None:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no pinned upstream request mapping",
            )
        if durations is not None and len(durations) != len(materials):
            raise BridgeContractError(
                "duration_count_mismatch",
                "Every material must carry its approved scene duration",
            )

        uploaded: list[dict] = []
        upload_endpoint = profile.endpoints.get("upload_material")
        if not upload_endpoint:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no upload_material endpoint",
            )

        for index, material in enumerate(materials):
            if not material.is_file():
                raise BridgeContractError(
                    "asset_unreadable",
                    f"Scene asset is not a readable file: {material.name}",
                )

            # §9.4: every material keeps the approved duration of its scene so
            # the upstream receive window matches the approved timeline.
            scene_duration = durations[index] if durations is not None else None
            data_bytes = material.read_bytes()
            sha256 = hashlib.sha256(data_bytes).hexdigest()
            filename = material.name
            cache_key = f"{task_id}:{sha256}"

            if cache_key in self._upload_cache:
                cached = self._upload_cache[cache_key]
                uploaded.append({
                    "provider": "local",
                    "url": cached["url"],
                    "duration": scene_duration,
                    "sha256": sha256,
                    "original_name": filename,
                })
                continue

            try:
                # The wrapper is the only reachable endpoint and it builds the
                # multipart envelope the pinned API expects, so the clip travels
                # as raw bytes with its name in a header.
                response = self._transport.post(
                    f"{self._url}{upload_endpoint}",
                    headers=self._headers({"x-vertep-filename": filename}),
                    content=data_bytes,
                    timeout=300,
                )
                _check(response)
                result = response.json()
                # Pinned upstream returns {status, message, data:{file:
                # <immutable UUID storage key>}}. The storage key — not a
                # filename — is what the submit request references, so the
                # exact key of the real envelope is asserted here.
                data = result.get("data") if isinstance(result, dict) else None
                if not isinstance(data, dict) or "file" not in data:
                    raise BridgeContractError(
                        "upstream_upload_failed",
                        f"Upload response missing data.file for {filename}",
                    )
                stored_file = data["file"]
                if not isinstance(stored_file, str) or not stored_file.strip():
                    raise BridgeContractError(
                        "upstream_upload_failed",
                        f"Upload returned an empty data.file for {filename}",
                    )
                entry = {
                    "provider": "local",
                    "url": stored_file,
                    "duration": scene_duration,
                    "sha256": sha256,
                    "original_name": filename,
                }
                self._upload_cache[cache_key] = entry
                uploaded.append(entry)
            except Exception as err:
                raise BridgeContractError(
                    "upstream_upload_failed",
                    f"Failed to upload {filename}: {err}",
                ) from err

        return uploaded

    def health_check(self) -> dict:
        """Check MPT runtime eligibility and readiness (Issue #122 P2, §9.12).

        The configured URL points at the Vertep wrapper, which is the only endpoint
        of the pinned runtime. Readiness is therefore decided by what the wrapper
        proves about the real executor, not by the fact that something answered:

        * a verified runtime snapshot — pinned commit, immutable image digest and
          bridge schema version (``engine_snapshot_mismatch`` on drift);
        * an upstream that answers 200 **with** ``x-api-key`` and refuses without it
          (``upstream_unreachable`` / ``upstream_unauthenticated``);
        * a submit schema that still declares every required ``TaskVideoRequest``
          field (``upstream_schema_unsupported``).

        Every one of these is mandatory. An unreachable, unverifiable or drifted
        runtime yields ``available`` False with an explicit reason — Issue #122
        forbids a silent fallback to the native engine for a configured engine.

        Returns:
            {
                "available": bool,
                "endpoint_reachable": bool,
                "schema_compatible": bool,
                "auth_enforced": bool,
                "bridge_version": str,
                "pinned_upstream": str,
                "runtime_manifest": dict | None,
                "snapshot_verified": bool,
                "reason": str | None,
                "error": str | None,
            }
        """
        result = {
            "available": False,
            "endpoint_reachable": False,
            "schema_compatible": False,
            "auth_enforced": False,
            "bridge_version": BRIDGE_SCHEMA_VERSION,
            "pinned_upstream": None,
            "runtime_manifest": None,
            "snapshot_verified": False,
            "reason": None,
            "error": None,
            "upstream_commit": None,
            "image_digest": None,
            "bridge_schema_version": None,
        }

        if not self._url:
            result["reason"] = "engine_not_configured"
            result["error"] = f"{self.env_url} not configured"
            return result

        profile = self.contract_profile
        if profile:
            result["pinned_upstream"] = profile.upstream_reference

        try:
            response = self._transport.get(
                f"{self._url}/health", headers=self._headers(), timeout=30
            )
            if not hasattr(response, "status_code"):
                raise TypeError(f"transport returned {type(response).__name__}, not a response")
            body = response.json()
        except Exception as err:  # noqa: BLE001 - any failure means "not ready"
            result["reason"] = "wrapper_unreachable"
            result["error"] = f"{type(err).__name__}: {err}"
            return result

        if getattr(response, "status_code", None) != 200 or not isinstance(body, dict):
            # The wrapper reports its own stable reason code when it refuses.
            checks = body.get("checks") if isinstance(body, dict) else None
            reported = (checks or {}).get("reason") if isinstance(checks, dict) else None
            result["reason"] = reported or "wrapper_unavailable"
            result["error"] = (
                (checks or {}).get("detail") if isinstance(checks, dict) else None
            ) or f"wrapper answered {getattr(response, 'status_code', 'no status')}"
            return result

        checks = body.get("checks")
        if not isinstance(checks, dict):
            result["reason"] = "wrapper_unavailable"
            result["error"] = "wrapper readiness report has no checks"
            return result

        result["endpoint_reachable"] = checks.get("upstream_auth_enforced") is True
        result["auth_enforced"] = checks.get("upstream_auth_enforced") is True
        if not result["endpoint_reachable"]:
            result["reason"] = checks.get("reason") or REASON_UPSTREAM_UNAUTHENTICATED
            result["error"] = checks.get("detail") or "upstream did not prove authenticated access"
            return result

        schema = checks.get("submit_schema")
        result["schema_compatible"] = isinstance(schema, (list, dict)) and set(
            REQUIRED_SUBMIT_FIELDS
        ).issubset(schema)
        if not result["schema_compatible"]:
            result["reason"] = REASON_SCHEMA_UNSUPPORTED
            result["error"] = (
                "wrapper did not prove a submit schema containing "
                f"{', '.join(REQUIRED_SUBMIT_FIELDS)}"
            )
            return result

        snapshot = checks.get("snapshot")
        result["runtime_manifest"] = snapshot if isinstance(snapshot, dict) else None
        if not isinstance(snapshot, dict):
            result["reason"] = REASON_INVENTORY_UNVERIFIED
            result["error"] = "wrapper did not report a verified runtime inventory"
            return result

        if not self._verify_reported_manifest(snapshot, profile, result):
            return result

        result["available"] = True
        return result

    @staticmethod
    def _verify_reported_manifest(snapshot: dict, profile, result: dict) -> bool:
        """Validate a reported runtime snapshot against the pinned contract.

        Returns False and records the reason in ``result`` when the runtime drifted,
        so the caller stops with ``available`` cleared.
        """
        expected_commit = profile.upstream_reference.split("@")[-1] if profile else None
        reported_commit = snapshot.get("upstream_commit")
        result["upstream_commit"] = reported_commit
        result["image_digest"] = snapshot.get("image_digest")
        result["bridge_version"] = snapshot.get("bridge_version", BRIDGE_SCHEMA_VERSION)
        result["bridge_schema_version"] = snapshot.get("bridge_schema_version")

        if expected_commit and reported_commit != expected_commit:
            result["reason"] = REASON_INVENTORY_UNVERIFIED
            result["error"] = (
                f"pinned runtime commit drift: expected {expected_commit}, "
                f"found {reported_commit}"
            )
            result["snapshot_verified"] = False
            return False

        digest = snapshot.get("image_digest")
        if not (isinstance(digest, str) and digest.startswith("sha256:")
                and len(digest) == len("sha256:") + 64):
            result["reason"] = REASON_SNAPSHOT_MISMATCH
            result["error"] = "runtime did not report an immutable image digest"
            result["snapshot_verified"] = False
            return False

        if snapshot.get("bridge_schema_version") != BRIDGE_SCHEMA_VERSION:
            result["reason"] = REASON_SNAPSHOT_MISMATCH
            result["error"] = (
                f"expected bridge schema {BRIDGE_SCHEMA_VERSION}, "
                f"runtime reported {snapshot.get('bridge_schema_version')}"
            )
            result["snapshot_verified"] = False
            return False

        result["snapshot_verified"] = True
        return True

    def verify_local_runtime_manifest(self, path) -> dict:
        """Verify a pinned runtime inventory read from disk (Issue #122 P2).

        Used by operators and by the container self-test that has direct filesystem
        access to the image. Raises ``RuntimeManifestError`` on any drift so the
        caller refuses the engine rather than degrading.
        """
        profile = self.contract_profile
        if profile is None:
            raise RuntimeManifestError(f"{self.name} has no pinned upstream contract")
        manifest = verify_inventory(
            path,
            root=Path(path).parent,
            expected_commit=profile.upstream_reference.split("@")[-1],
            expected_bridge_version=BRIDGE_SCHEMA_VERSION,
            expected_bridge_schema_version=BRIDGE_SCHEMA_VERSION,
        )
        return {
            "upstream_reference": manifest.upstream_reference,
            "upstream_version": manifest.upstream_version,
            "bridge_version": manifest.bridge_version,
            "bridge_schema_version": manifest.bridge_schema_version,
            "dependency_count": len(manifest.dependencies),
            "inventory_digest": manifest.compute_digest(),
        }


class ShortGPTEngine(RemoteVideoEngine):
    """External ShortGPT-style engine (MIT reference) behind VideoEngine."""

    name = "shortgpt"
    env_url = "SHORTGPT_URL"
    env_token = "SHORTGPT_TOKEN"
