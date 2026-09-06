"""Tests for the formalized Provider Layer (Phase 1)."""

from adapters.publisher import PUBLISHERS

from adapters.providers import (
    providers,
    get_providers,
    ProviderRegistry,
    DefaultAssemblyProvider,
    DefaultComputeProvider,
    DefaultImageProvider,
    DefaultLLMProvider,
    DefaultPublisherProvider,
    DefaultTTSProvider,
    DefaultVideoProvider,
    LLMProvider,
    ImageProvider,
    VideoProvider,
    TTSProvider,
    AssemblyProvider,
    ComputeProvider,
    PublisherProvider,
)


def test_registry_returns_default_implementations():
    reg = get_providers()
    assert isinstance(reg.llm(), LLMProvider)
    assert isinstance(reg.image(), ImageProvider)
    assert isinstance(reg.video(), VideoProvider)
    assert isinstance(reg.compute(), ComputeProvider)
    assert isinstance(reg.assembly(), AssemblyProvider)
    assert isinstance(reg.publisher(), PublisherProvider)
    tts = reg.tts("mock")
    assert isinstance(tts, TTSProvider)
    assert tts.provider == "mock"


def test_all_provider_types_are_abc_conformant():
    # Default implementations must be instances of their ABC interfaces.
    assert isinstance(DefaultLLMProvider(object()), LLMProvider)
    assert isinstance(DefaultComputeProvider(object()), ComputeProvider)
    assert isinstance(DefaultImageProvider(object()), ImageProvider)
    assert isinstance(DefaultVideoProvider(object()), VideoProvider)
    assert isinstance(DefaultAssemblyProvider(object()), AssemblyProvider)
    assert isinstance(DefaultPublisherProvider(), PublisherProvider)
    assert isinstance(DefaultTTSProvider(object()), TTSProvider)


def test_tts_factory_yields_independent_instances():
    reg = get_providers()
    first = reg.tts("none")
    second = reg.tts("mock")
    # Each call returns a new provider bound to its own backend.
    assert first.provider == "none"
    assert second.provider == "mock"


def test_tts_provider_default_mock(tmp_path):
    output = tmp_path / "voice.wav"
    tts = get_providers().tts("mock")
    result = tts.synthesize("Привіт", output, 0.2, {"voice": "mock"})
    assert result["status"] == "READY"
    assert output.stat().st_size > 100


def test_assembly_provider_delegates_to_ffmpeg(tmp_path):
    image = tmp_path / "src.ppm"
    image.write_bytes(b"P6\n4 4\n255\n" + bytes((10, 20, 30)) * 16)
    output = get_providers().assembly().assemble(
        tmp_path / "out.mp4", images=[image], durations=[0.2], aspect_ratio="9:16"
    )
    assert output.stat().st_size > 100
    assert b"ftyp" in output.read_bytes()[:32]
    probe = get_providers().assembly().probe(output)
    assert isinstance(probe, dict)


def test_publisher_provider_wraps_publishers(monkeypatch):
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    pub = get_providers().publisher()
    assert set(pub.available_channels()) == set(PUBLISHERS.keys())
    assert pub.configured("youtube") is False
    result = pub.publish("youtube", "", {"topic": "x"})
    assert result["status"] == "NOT_CONFIGURED"


def test_provider_registry_replace_swaps_implementation():
    reg = ProviderRegistry()

    class Fake:
        def generate_script(self, topic, system_prompt="", character=None):
            return {"title": topic, "scenes": []}

    fake = Fake()
    reg.register("llm", fake)
    assert reg.llm() is fake

    other = Fake()
    reg.replace("llm", other)
    assert reg.llm() is other