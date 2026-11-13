"""Pinned MoneyPrinterTurbo bridge contract profile."""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)

from ..runtime_manifest import PINNED_UPSTREAM_REFERENCE


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
