"""Native Vertep video engine: FFmpeg assembly through ``AssemblyProvider``."""

from __future__ import annotations

from pathlib import Path

from ..base import (
    RELEASE_NOT_APPLIED,
    VideoEngine,
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

    def release_state(self, submit_key: str | None = None) -> str:
        """A local render holds no remote runtime, so no remote lease is involved.

        The Native route assembles Vertep's own assets in this process; there is no
        pinned upstream that could keep rendering after a cancel, so the only thing a
        cancel has to fence is the attempt itself (§5, §9.8).
        """
        return RELEASE_NOT_APPLIED

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
