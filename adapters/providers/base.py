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
    ) -> Path:
        ...
