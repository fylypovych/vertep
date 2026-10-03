"""Abstract interfaces for all external backend providers.

Each provider defines a formal contract that decouples pipeline/worker code
from concrete adapter implementations.  Default implementations wrap the
existing adapters (ComfyUIAdapter, TTSAdapter, FFmpegAdapter, etc.) and can
be swapped for alternative backends without touching orchestration logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class LLMProvider(ABC):
    """LLM-based text / script generation."""

    @abstractmethod
    def generate_script(
        self,
        topic: str,
        system_prompt: str = "",
        character: dict | None = None,
    ) -> dict:
        ...


class ImageProvider(ABC):
    """AI image generation (e.g. Stable Diffusion via ComfyUI)."""

    @abstractmethod
    def generate(self, workflow_path: str, topic: str) -> tuple[bytes, str]:
        ...


class VideoProvider(ABC):
    """AI video generation (e.g. video models via ComfyUI)."""

    @abstractmethod
    def generate(self, workflow_path: str, topic: str) -> tuple[bytes, str]:
        ...


class TTSProvider(ABC):
    """Text-to-speech synthesis."""

    @property
    @abstractmethod
    def provider(self) -> str:
        ...

    @abstractmethod
    def configured(self) -> bool:
        ...

    @abstractmethod
    def synthesize(
        self,
        text: str,
        output: Path,
        duration: float = 3.0,
        voice: dict | None = None,
    ) -> dict:
        ...


class AssemblyProvider(ABC):
    """Video / audio assembly (e.g. FFmpeg)."""

    @abstractmethod
    def assemble(
        self,
        output: Path,
        image: Path | None = None,
        images: list[Path] | None = None,
        durations: list[float] | None = None,
        audio: Path | None = None,
        music: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
        watermark: Path | None = None,
    ) -> Path:
        ...

    @abstractmethod
    def assemble_clips(
        self,
        output: Path,
        clips: list[Path],
        audio: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
    ) -> Path:
        ...

    @abstractmethod
    def concat_audio(self, sources: list[Path], output: Path) -> Path:
        ...

    @abstractmethod
    def pre_cut(self, source: Path, duration: float, output: Path) -> Path:
        """Cut one approved scene asset to exactly ``duration`` seconds.

        Issue #122 §9.3: an external engine receives one clip per scene and
        has no per-item duration, so the approved scene duration is produced
        here, before the asset leaves Vertep. The result is a silent, constant
        frame-rate MP4 so the external engine can concatenate it.
        """
        ...

    @abstractmethod
    def apply_post_step(
        self,
        output: Path,
        video: Path,
        *,
        audio: Path | None = None,
        music: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
        watermark: Path | None = None,
    ) -> Path:
        """Finish a video produced by an external engine (Issue #122 §9.5).

        Applies the audio mix, the conditional subtitle burn, the watermark
        overlay and the preset geometry/fps in the same order and with the same
        filter parameters the Native assembly uses, so the final artifact is
        geometrically and audibly identical to a Native render.
        """
        ...

    @abstractmethod
    def probe(self, path: Path) -> dict:
        ...


class ComputeProvider(ABC):
    """GPU compute workflows (e.g. ComfyUI)."""

    @abstractmethod
    def generate_output(
        self, workflow_path: str, topic: str, task_type: str = "image"
    ) -> tuple[bytes, str, str]:
        ...

    @abstractmethod
    def cancel(self) -> bool:
        """Cancel only this provider's own work.

        Returns ``True`` when the work was actually stopped/removed and ``False``
        when it could only be abandoned.  Implementations must never escalate to a
        backend-wide stop that would abort work owned by somebody else.
        """
        ...


class PublisherProvider(ABC):
    """Content publishing to external platforms."""

    @abstractmethod
    def configured(self, channel: str) -> bool:
        ...

    @abstractmethod
    def publish(self, channel: str, video_path: str, metadata: dict) -> dict:
        ...

    @abstractmethod
    def available_channels(self) -> list[str]:
        ...

    def ready(self, channel: str) -> bool:
        """Return True when the channel credentials are present and usable."""
        ...

    def missing_scopes(self, channel: str) -> list[str]:
        """Return OAuth scopes missing for the channel."""
        ...

    def reconnect(self, channel: str) -> bool:
        """Trigger re-authentication for the given channel."""
        ...


BRIDGE_SCHEMA_VERSION = "v1"

# The bridge release itself, as opposed to the schema version of a single call.
# Both are recorded in the runtime inventory and verified by the container
# entrypoint before the pinned upstream is started, so a mismatched image can
# never report itself ready. The image build defaults its ARGs to these values.
BRIDGE_VERSION = "v1"

# Issue #122 §9.12: the pinned upstream submit schema must still declare every one
# of these fields, otherwise the bridge cannot build a request and the runtime is
# refused. The wrapper proves this against the real ``TaskVideoRequest`` inside the
# container; the engine consumes the same list when reading that proof.
#
# The list covers every input the bridge actually submits, not only the ones needed to
# start a task: §9.3 requires the deterministic script (``video_script``), the staged
# scene clips (``video_materials``) and the approved voice (``custom_audio_file``) to
# reach the pinned engine. A runtime that stopped accepting any of them could still
# accept a bare subject and would then silently regenerate content the factory owns.
REQUIRED_SUBMIT_FIELDS = (
    "video_subject",
    "video_script",
    "video_materials",
    "custom_audio_file",
    "video_aspect",
    "video_source",
    "subtitle_enabled",
    "video_clip_duration",
)

# Stable readiness refusal codes (Issue #122 §9.12). They are contract, not log
# text: the engine, the wrapper and the executor must agree on them so a refused
# runtime is reported with one reason instead of a generic "unavailable".
REASON_UPSTREAM_UNREACHABLE = "upstream_unreachable"
REASON_UPSTREAM_UNAUTHENTICATED = "upstream_unauthenticated"
REASON_SCHEMA_UNSUPPORTED = "upstream_schema_unsupported"
REASON_FFMPEG_UNAVAILABLE = "ffmpeg_unavailable"
REASON_MEDIA_PIPELINE_FAILED = "media_pipeline_unavailable"
REASON_INVENTORY_UNVERIFIED = "runtime_inventory_unverified"
REASON_SNAPSHOT_MISMATCH = "engine_snapshot_mismatch"
# The pinned runtime cannot receive a pre-rendered voice file, so an external
# render that would have to make the runtime synthesise its own narration is
# refused instead of accepted (Issue #122 §9.6, §9.10).
REASON_VOICE_STAGING_UNSUPPORTED = "upstream_voice_staging_unsupported"
# A submit response was lost and the runtime cannot prove whether the upstream
# accepted the work. The attempt is refused as explicitly unknown instead of being
# resubmitted blindly or reported as success (Issue #122 §5, §9.8).
REASON_SUBMIT_UNKNOWN = "upstream_submit_unknown"


class BridgeContractError(ValueError):
    """Contract violation detected before an external engine is called.

    External engines declare what they can actually honour (Issue #122, P1).
    A rejected input must fail here — before dispatch, with an explicit reason —
    instead of being ignored upstream or silently degrading to another engine.
    ``code`` is the stable rejection code recorded in Job/task state.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VideoEngine(ABC):
    """High-level video assembly driver (not to be confused with ``VideoProvider``).

    ``VideoProvider`` *generates* raw AI clips/frames; ``VideoEngine`` *renders*
    the final video from already-produced assets (images/clips, voice, music,
    subtitles). ``NativeVertepEngine`` wraps the current FFmpeg assembly pipeline
    and is the default; external engines (MoneyPrinter / ShortGPT style) can be
    plugged behind the same interface without Vertep giving up Job-state or
    character metadata — Vertep always owns the Job lifecycle, the engine only
    renders.
    """

    @abstractmethod
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
        ...

    @property
    def engine_id(self) -> str:
        """Stable identifier of the effective engine, used in snapshots (§9.15)."""
        return self.provider

    def cancel(self, job_id: str | None = None, *, submit_key: str | None = None) -> bool:
        """Ask the engine runtime to stop one attempt it owns.

        Returns ``True`` only when the runtime confirmed the stop and ``False``
        when the work could only be abandoned or is still running.  An engine that
        cannot cancel reports ``False`` instead of raising: CORE fences the attempt
        logically and discards any late result (Issue #122 §5, §9.8).
        """
        return False
