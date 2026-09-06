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

import os
import time
from pathlib import Path

from ._http import check_response as _check
from .base import VideoEngine
from publishers.transport import HttpTransport


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
    ) -> dict:
        return {
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
        output.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            self._wait_for_status(job_id)
        finally:
            self.current_job_id = None
        download = self._transport.get(
            f"{self._url}/download/{job_id}",
            headers=self._headers(),
            timeout=180,
        )
        _check(download)
        output.write_bytes(download.content)
        return output

    def _wait_for_status(self, job_id: str) -> dict:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            response = self._transport.get(
                f"{self._url}/jobs/{job_id}",
                headers=self._headers(),
                timeout=10,
            )
            _check(response)
            status = response.json()
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


class ShortGPTEngine(RemoteVideoEngine):
    """External ShortGPT-style engine (MIT reference) behind VideoEngine."""

    name = "shortgpt"
    env_url = "SHORTGPT_URL"
    env_token = "SHORTGPT_TOKEN"