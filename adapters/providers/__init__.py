"""Provider Layer — formalized interfaces for all external backends.

Usage::

    from adapters.providers import providers

    tts = providers.tts(provider_name="mock")
    tts.synthesize("text", output_path, 3.0)

    assembly = providers.assembly()
    assembly.assemble(output, images=images, durations=durations)

    compute = providers.compute()
    data, filename, kind = compute.generate_output(workflow, topic, "image")
"""

from __future__ import annotations

import os
from typing import Any
from pathlib import Path

from .base import (
    AssemblyProvider,
    ComputeProvider,
    ImageProvider,
    LLMProvider,
    PublisherProvider,
    TTSProvider,
    VideoEngine,
    VideoProvider,
)
from .compute_backends import ComfyUIDistributedProvider
from .video_engines import MoneyPrinterEngine, NativeVertepEngine, ShortGPTEngine


# ---------------------------------------------------------------------------
# Default implementations (thin wrappers around existing adapters)
# ---------------------------------------------------------------------------


class DefaultLLMProvider(LLMProvider):
    """Default LLM provider backed by ScriptAgent."""

    def __init__(self, script_agent: Any) -> None:
        self._agent = script_agent

    def generate_script(
        self,
        topic: str,
        system_prompt: str = "",
        character: dict | None = None,
    ) -> dict:
        return self._agent.generate_script(topic, system_prompt, character)


class DefaultImageProvider(ImageProvider):
    """Default image provider backed by ComfyUIAdapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def generate(self, workflow_path: str, topic: str) -> tuple[bytes, str]:
        return self._adapter.generate(workflow_path, topic)


class DefaultVideoProvider(VideoProvider):
    """Default video provider backed by ComfyUIAdapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def generate(self, workflow_path: str, topic: str) -> tuple[bytes, str]:
        data, filename, kind = self._adapter.generate_output(
            workflow_path, topic, "video"
        )
        if kind != "video":
            raise RuntimeError(f"Video provider returned unexpected kind: {kind}")
        return data, filename


class DefaultTTSProvider(TTSProvider):
    """Default TTS provider backed by TTSAdapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    @property
    def provider(self) -> str:
        return self._adapter.provider

    def configured(self) -> bool:
        return self._adapter.configured()

    def synthesize(
        self,
        text: str,
        output: Path,
        duration: float = 3.0,
        voice: dict | None = None,
    ) -> dict:
        return self._adapter.synthesize(text, output, duration, voice)


class DefaultAssemblyProvider(AssemblyProvider):
    """Default assembly provider backed by FFmpegAdapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

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
        return self._adapter.assemble(
            output, image=image, images=images, durations=durations,
            audio=audio, music=music, subtitles=subtitles,
            aspect_ratio=aspect_ratio, preset=preset, watermark=watermark,
        )

    def assemble_clips(
        self,
        output: Path,
        clips: list[Path],
        audio: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
    ) -> Path:
        return self._adapter.assemble_clips(
            output, clips, audio=audio, subtitles=subtitles,
            aspect_ratio=aspect_ratio, preset=preset,
        )

    def concat_audio(self, sources: list[Path], output: Path) -> Path:
        return self._adapter.concat_audio(sources, output)

    def probe(self, path: Path) -> dict:
        return self._adapter.probe(path)

class DefaultComputeProvider(ComputeProvider):
    """Default compute provider backed by ComfyUIAdapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def generate_output(
        self, workflow_path: str, topic: str, task_type: str = "image"
    ) -> tuple[bytes, str, str]:
        return self._adapter.generate_output(workflow_path, topic, task_type)

    def cancel(self) -> bool:
        return self._adapter.cancel()


class DefaultPublisherProvider(PublisherProvider):
    """Default publisher provider backed by the PUBLISHERS registry."""

    def configured(self, channel: str) -> bool:
        from adapters.publisher import PUBLISHERS

        pub = PUBLISHERS.get(channel)
        return pub is not None and pub.configured()

    def publish(self, channel: str, video_path: str, metadata: dict) -> dict:
        from adapters.publisher import PUBLISHERS

        pub = PUBLISHERS.get(channel)
        if pub is None:
            return {
                "channel": channel,
                "status": "FAILED",
                "error": f"Unknown channel: {channel}",
            }
        return pub.publish(video_path, metadata)

    def available_channels(self) -> list[str]:
        from adapters.publisher import PUBLISHERS

        return list(PUBLISHERS.keys())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """Central registry for provider implementations.

    Supports both pre-built instances and factory callables (for providers
    that require per-call configuration, e.g. TTS with different backends).
    """

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(self, name: str, provider: Any) -> None:
        """Register a provider instance or factory callable."""
        self._providers[name] = provider

    # -- typed accessors ----------------------------------------------------

    def llm(self) -> LLMProvider:
        return self._providers["llm"]

    def image(self) -> ImageProvider:
        return self._providers["image"]

    def video(self) -> VideoProvider:
        return self._providers["video"]

    def tts(self, provider_name: str | None = None, **kwargs: Any) -> TTSProvider:
        entry = self._providers.get("tts")
        if entry is None:
            raise LookupError("TTS provider not registered")
        if callable(entry) and not isinstance(entry, type):
            return entry(provider_name, **kwargs)
        return entry  # type: ignore[return-value]

    def assembly(self) -> AssemblyProvider:
        return self._providers["assembly"]

    def compute(self) -> ComputeProvider:
        return self._providers["compute"]

    def publisher(self) -> PublisherProvider:
        return self._providers["publisher"]

    def video_engine(self) -> VideoEngine:
        return self._providers["video_engine"]

    def replace(self, name: str, provider: Any) -> None:
        """Replace a registered provider (for testing / runtime swaps)."""
        self._providers[name] = provider


# ---------------------------------------------------------------------------
# Default wiring
# ---------------------------------------------------------------------------


def _create_default_registry() -> ProviderRegistry:
    """Build a registry with the default backend implementations."""
    registry = ProviderRegistry()

    # LLM — wrap ScriptAgent (the actual pipeline LLM orchestrator).
    # Lazy import to avoid circular dependency with core package.
    from core.script_agent import ScriptAgent

    from adapters.comfyui import ComfyUIAdapter
    from adapters.ffmpeg import FFmpegAdapter
    from adapters.tts import TTSAdapter

    registry.register("llm", DefaultLLMProvider(ScriptAgent()))

    comfyui = ComfyUIAdapter()
    registry.register("compute", _make_compute())
    registry.register("image", DefaultImageProvider(comfyui))
    registry.register("video", DefaultVideoProvider(comfyui))

    # TTS needs a factory because provider_name varies per character/job.
    registry.register("tts", _make_tts)

    registry.register("assembly", DefaultAssemblyProvider(FFmpegAdapter()))
    registry.register("video_engine", _make_video_engine())
    registry.register("publisher", DefaultPublisherProvider())

    return registry


def _make_compute() -> ComputeProvider:
    """Resolve the compute backend.

    Default is the VertepWorker provider (attached ComfyUIAdapter). The
    ComfyUI-Distributed cluster is used only on explicit opt-in
    (``VERTEP_COMPUTE_PROVIDER=comfyui-distributed``) when a proxy URL is
    configured; otherwise it falls back to the VertepWorker provider.
    """
    from adapters.comfyui import ComfyUIAdapter
    from adapters.providers.compute_backends import ComfyUIDistributedProvider

    provider_name = (
        os.getenv("VERTEP_COMPUTE_PROVIDER", "vertep-worker").lower().strip()
    )
    if provider_name == "comfyui-distributed":
        distributed = ComfyUIDistributedProvider()
        if distributed.configured():
            return distributed
        # Not configured → fall back to VertepWorker.
    return DefaultComputeProvider(ComfyUIAdapter())


def _make_video_engine() -> VideoEngine:
    """Resolve the video assembly engine.

    Default is the native Vertep FFmpeg pipeline (``NativeVertepEngine``).
    External engines (MoneyPrinter / ShortGPT) are used only on explicit opt-in
    via ``VERTEP_VIDEO_ENGINE`` **and** a configured render URL; otherwise the
    factory falls back to the native engine.
    """
    engine_name = (
        os.getenv("VERTEP_VIDEO_ENGINE", "native").lower().strip()
    )
    if engine_name == "money-printer":
        engine = MoneyPrinterEngine()
        if engine.configured():
            return engine
    elif engine_name == "shortgpt":
        engine = ShortGPTEngine()
        if engine.configured():
            return engine
    return NativeVertepEngine()


def _make_tts(provider_name: str | None = None, **kwargs: Any) -> TTSProvider:
    """Resolve the TTS backend for a provider name.

    Live open-source engines (Piper / Kokoro) are returned directly; everything
    else falls back to the legacy ``TTSAdapter`` (mock / none / placeholder).
    """
    from adapters.providers.tts_backends import KokoroProvider, PiperProvider
    from adapters.tts import TTSAdapter

    name = (provider_name or os.getenv("TTS_PROVIDER", "none")).lower().strip()
    if name == "piper":
        return PiperProvider()
    if name == "kokoro":
        return KokoroProvider()
    return DefaultTTSProvider(TTSAdapter(provider_name))
def provider_matrix() -> dict[str, dict]:
    """Describe the currently active backend matrix (no network I/O).

    Used by the Web UI (Settings → Engines) and ``/api/status``. Each slot
    exposes:

    - ``backend`` — active backend name;
    - ``options`` — known alternative backends;
    - ``env`` — controlling environment variable;
    - ``configured`` — whether the active backend is considered configured.

    The publisher slot additionally exposes per-platform ``configured`` flags
    under ``platforms``. All checks are local (env / object state), no HTTP.
    """
    from adapters.providers.compute_backends import ComfyUIDistributedProvider
    from adapters.providers.tts_backends import KokoroProvider, PiperProvider
    from adapters.providers.video_engines import MoneyPrinterEngine, ShortGPTEngine

    matrix: dict[str, dict] = {}

    # LLM
    llm_name = os.getenv("VERTEP_LLM_PROVIDER", "ollama").lower().strip()
    llm_configured = True
    if llm_name == "openai":
        llm_configured = bool(
            os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        )
    matrix["llm"] = {
        "backend": "openai" if llm_name == "openai" else "ollama",
        "options": ["ollama", "openai"],
        "env": "VERTEP_LLM_PROVIDER",
        "configured": llm_configured,
    }

    # TTS — default backend from env (per-character provider may override).
    tts_name = os.getenv("TTS_PROVIDER", "none").lower().strip()
    if tts_name == "piper":
        tts_backend, tts_configured = "piper", PiperProvider().configured()
    elif tts_name == "kokoro":
        tts_backend, tts_configured = "kokoro", KokoroProvider().configured()
    else:
        tts_backend, tts_configured = tts_name, True
    matrix["tts"] = {
        "backend": tts_backend,
        "options": ["none", "mock", "piper", "kokoro"],
        "env": "TTS_PROVIDER",
        "configured": tts_configured,
    }

    # Compute — GPU image/video share the same backend.
    compute_name = (
        os.getenv("VERTEP_COMPUTE_PROVIDER", "vertep-worker").lower().strip()
    )
    if compute_name == "comfyui-distributed":
        dist_configured = ComfyUIDistributedProvider().configured()
        compute_active = (
            "comfyui-distributed" if dist_configured else "vertep-worker"
        )
        compute_configured = dist_configured
    else:
        compute_active, compute_configured = "vertep-worker", True
    compute_slot = {
        "backend": compute_active,
        "options": ["vertep-worker", "comfyui-distributed"],
        "env": "VERTEP_COMPUTE_PROVIDER",
        "configured": compute_configured,
    }
    matrix["compute"] = compute_slot
    matrix["image"] = dict(compute_slot)
    matrix["video"] = dict(compute_slot)

    # Assembly — always the native FFmpeg pipeline.
    matrix["assembly"] = {
        "backend": "ffmpeg",
        "options": ["ffmpeg"],
        "env": "—",
        "configured": True,
    }

    # Video engine — final render driver (defaults to native).
    engine_name = (
        os.getenv("VERTEP_VIDEO_ENGINE", "native").lower().strip()
    )
    if engine_name == "money-printer":
        engine_configured = MoneyPrinterEngine().configured()
        engine_active = (
            "money-printer" if engine_configured else "native"
        )
    elif engine_name == "shortgpt":
        engine_configured = ShortGPTEngine().configured()
        engine_active = "shortgpt" if engine_configured else "native"
    else:
        engine_active, engine_configured = "native", True
    matrix["video_engine"] = {
        "backend": engine_active,
        "options": ["native", "money-printer", "shortgpt"],
        "env": "VERTEP_VIDEO_ENGINE",
        "configured": engine_configured,
    }

    # Publisher — official platform adapters behind PublisherProvider.
    publisher = DefaultPublisherProvider()
    publisher_options: dict[str, dict] = {}
    for channel in publisher.available_channels():
        publisher_options[channel] = {
            "configured": publisher.configured(channel)
        }
    matrix["publisher"] = {
        "backend": "vertep-official",
        "options": list(publisher_options),
        "env": "PUBLISHER_MOCK / platform credentials",
        "platforms": publisher_options,
    }

    return matrix


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_providers: ProviderRegistry | None = None


def get_providers() -> ProviderRegistry:
    """Return the global provider registry (created on first call)."""
    global _providers
    if _providers is None:
        _providers = _create_default_registry()
    return _providers


class _LazyProviders:
    """Proxy that defers registry creation until first attribute access."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_providers(), name)


providers: ProviderRegistry = _LazyProviders()  # type: ignore[assignment]


__all__ = [
    "providers",
    "get_providers",
    "provider_matrix",
    "ProviderRegistry",
    # ABCs
    "LLMProvider",
    "ImageProvider",
    "VideoProvider",
    "TTSProvider",
    "AssemblyProvider",
    "ComputeProvider",
    "PublisherProvider",
    "VideoEngine",
    # Defaults
    "DefaultLLMProvider",
    "DefaultImageProvider",
    "DefaultVideoProvider",
    "DefaultTTSProvider",
    "DefaultAssemblyProvider",
    "DefaultComputeProvider",
    "DefaultPublisherProvider",
    "ComfyUIDistributedProvider",
    "NativeVertepEngine",
    "MoneyPrinterEngine",
    "ShortGPTEngine",
]
