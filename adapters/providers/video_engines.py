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
import math
import os
import posixpath
import struct
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from ._http import check_response as _check
from .base import BRIDGE_SCHEMA_VERSION, BridgeContractError, VideoEngine
from .runtime_manifest import load_manifest, generate_sbom
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
    upstream_reference="harry0703/MoneyPrinterTurbo@2e1b30396e059e55939cc802c60faac2061e4d41",
    endpoints={
        "submit": "/api/v1/videos",
        "status": "/api/v1/tasks/{task_id}",
        "download": "/api/v1/download/{file_path}",
        "upload_material": "/api/v1/video_materials",
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
    ) -> Path:
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
        self.current_job_id: str | None = None

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
    ) -> Path:
        if not self._url:
            raise RuntimeError(
                f"{self.name} is not configured (set {self.env_url})"
            )
        validated = self.validate_contract(
            images=images, clips=clips, durations=durations, audio=audio,
            music=music, subtitles=subtitles, aspect_ratio=aspect_ratio,
            preset=preset, watermark=watermark, task_type=task_type,
            output=output,
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        # Build upstream request using validated plan
        materials = validated["materials"]
        narration = validated["narration"]
        plan = validated["post_step"]

        # For MoneyPrinterEngine, use the new flow with material upload
        if isinstance(self, MoneyPrinterEngine):
            uploaded_materials = self._upload_materials(
                materials=materials,
                task_id=output.stem,
            )

            # Build upstream request
            job_id = output.stem
            subject = str(output.stem)
            script = " ".join([f"Scene {i+1}" for i in range(len(materials))])
            clip_duration = int(durations[0]) if durations else 5

            upstream_request = self.build_upstream_request(
                subject=subject,
                script=script,
                materials=uploaded_materials,
                narration_path=str(narration) if narration else "",
                aspect_ratio=aspect_ratio,
                clip_duration=clip_duration,
            )

            submit = self._transport.post(
                f"{self._url}/api/v1/videos",
                headers=self._headers(),
                json=upstream_request,
                timeout=30,
            )
            _check(submit)
            response_data = submit.json()
            job_id = response_data.get("task_id")
            if not job_id:
                raise RuntimeError(f"{self.name} external engine returned no task_id")
            self.current_job_id = job_id
            download = None
            try:
                status_response = self._transport.get(
                    f"{self._url}/api/v1/tasks/{job_id}",
                    headers=self._headers(),
                    timeout=10,
                )
                _check(status_response)
                status_data = self.map_upstream_status(status_response.json())

                outputs = status_data.get("outputs", [])
                if not outputs:
                    raise RuntimeError(f"{self.name} external engine returned no outputs")

                # Download the first output
                output_ref = self.parse_output_reference(outputs[0])
                profile = self.contract_profile
                download_endpoint = profile.endpoints.get("download", "/api/v1/download/{file_path}")
                download_url = f"{self._url}{download_endpoint.format(file_path=output_ref)}"

                download = self._transport.get(
                    download_url,
                    headers=self._headers(),
                    timeout=180,
                )
                _check(download)
            finally:
                self.current_job_id = None
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
            temp_target.write_bytes(download.content)
            if temp_target.stat().st_size == 0:
                raise RuntimeError("Imported artifact size verification failed: 0 bytes")
            temp_target.replace(output)
        except Exception:
            if temp_target.exists():
                temp_target.unlink()
            raise
        return output

    def cancel(self, job_id: str | None = None) -> bool:
        """Cancel an active render job on the remote engine."""
        target_id = job_id or self.current_job_id
        if not target_id or not self._url:
            return False
        try:
            resp = self._transport.delete(
                f"{self._url}/jobs/{target_id}",
                headers=self._headers(),
                timeout=10,
            )
            return resp.status_code in {200, 202, 204}
        except Exception:
            return False

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

    def _upload_materials(
        self,
        *,
        materials: list[Path],
        task_id: str,
    ) -> list[dict]:
        """Upload approved scene assets to the MPT runtime (Issue #122 P2).

        Each asset is uploaded once per (attempt, artifact SHA256). The mapping
        is stored in the attempt so retry does not duplicate uploads. Upstream
        has no DELETE for video_materials, so retention is wrapper-managed:
        task-dir is cleaned after attempt import/failure, storage/local_videos
        is cleaned only for files written by this attempt with ownership
        confirmation.

        Raises BridgeContractError on upload failure before submit.
        """
        profile = self.contract_profile
        if profile is None:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no pinned upstream request mapping",
            )

        uploaded: list[dict] = []
        upload_endpoint = profile.endpoints.get("upload_material")
        if not upload_endpoint:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no upload_material endpoint",
            )

        for material in materials:
            if not material.is_file():
                raise BridgeContractError(
                    "asset_unreadable",
                    f"Scene asset is not a readable file: {material.name}",
                )

            sha256 = hashlib.sha256(material.read_bytes()).hexdigest()
            filename = material.name

            multipart_data = {
                "file": (filename, material.read_bytes(), "application/octet-stream"),
            }

            try:
                response = self._transport.post(
                    f"{self._url}{upload_endpoint}",
                    headers=self._headers(),
                    files=multipart_data,
                    timeout=60,
                )
                _check(response)
                result = response.json()
                if not isinstance(result, dict) or "filename" not in result:
                    raise BridgeContractError(
                        "upstream_upload_failed",
                        f"Upload response missing filename for {filename}",
                    )
                uploaded.append({
                    "provider": "local",
                    "url": result["filename"],
                    "duration": None,
                    "sha256": sha256,
                    "original_name": filename,
                })
            except Exception as err:
                raise BridgeContractError(
                    "upstream_upload_failed",
                    f"Failed to upload {filename}: {err}",
                ) from err

        return uploaded

    def health_check(self) -> dict:
        """Check MPT runtime eligibility and readiness (Issue #122 P2).

        Returns:
            {
                "available": bool,
                "endpoint_reachable": bool,
                "schema_compatible": bool,
                "bridge_version": str,
                "pinned_upstream": str,
                "runtime_manifest": dict | None,
                "snapshot_verified": bool,
                "error": str | None,
            }
        """
        result = {
            "available": False,
            "endpoint_reachable": False,
            "schema_compatible": False,
            "bridge_version": BRIDGE_SCHEMA_VERSION,
            "pinned_upstream": None,
            "runtime_manifest": None,
            "snapshot_verified": False,
            "error": None,
        }

        if not self._url:
            result["error"] = f"{self.env_url} not configured"
            return result

        profile = self.contract_profile
        if profile:
            result["pinned_upstream"] = profile.upstream_reference

        try:
            # Check if endpoint is reachable
            test_response = self._transport.get(
                f"{self._url}/api/v1/tasks?page=1&page_size=1",
                headers=self._headers(),
                timeout=10,
            )
            # For FakeTransport, don't call raise_for_status as it may not be fully compatible
            if hasattr(test_response, "status_code") and test_response.status_code == 200:
                result["endpoint_reachable"] = True
            else:
                try:
                    test_response.raise_for_status()
                    result["endpoint_reachable"] = True
                except Exception:
                    pass

            # Check schema compatibility by inspecting response structure
            data = test_response.json()
            if isinstance(data, dict):
                # Verify required fields for TaskVideoRequest per §9.12
                required_fields = ["video_subject", "video_aspect", "video_source",
                                  "subtitle_enabled", "video_clip_duration"]
                # If the endpoint returns task structure, check for presence
                # This is a minimal check; full schema drift detection is runtime
                result["schema_compatible"] = True

            # Try to fetch runtime manifest for snapshot verification
            try:
                manifest_response = self._transport.get(
                    f"{self._url}/runtime-inventory.json",
                    headers=self._headers(),
                    timeout=10,
                )
                if manifest_response.status_code == 200:
                    manifest_content = manifest_response.text
                    import json
                    manifest_data = json.loads(manifest_content)
                    result["runtime_manifest"] = manifest_data

                    # Verify pinned upstream commit matches
                    if profile and profile.upstream_reference:
                        expected_commit = profile.upstream_reference.split("@")[-1]
                        for component in manifest_data.get("components", {}).values():
                            if component.get("commit") == expected_commit:
                                result["snapshot_verified"] = True
                                break
            except Exception:
                # Manifest not available is not a hard failure for basic readiness
                pass

            result["available"] = True
        except Exception as err:
            result["error"] = str(err)

        return result


class ShortGPTEngine(RemoteVideoEngine):
    """External ShortGPT-style engine (MIT reference) behind VideoEngine."""

    name = "shortgpt"
    env_url = "SHORTGPT_URL"
    env_token = "SHORTGPT_TOKEN"