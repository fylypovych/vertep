"""Tests for Module System (Stage 5-7, Issue #300 i.0.0.3.0).

ModuleRegistry, ModuleManifest, ModuleDiscovery — registration,
capability indexing, discovery, and module manifest lifecycle.
"""

from core.module.discovery import ModuleDiscovery
from core.module.manifest import ModuleManifest
from core.module.registry import ModuleRegistry


def test_module_manifest_creation_and_validation():
    manifest = ModuleManifest(
        name="text",
        version="0.0.3.0",
        capabilities=["text_generation", "script_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0, "cpu_cores": 1, "ram_mb": 512},
        description="Text module",
    )
    assert manifest.name == "text"
    assert manifest.version == "0.0.3.0"
    assert "text_generation" in manifest.capabilities
    assert manifest.to_dict()["name"] == "text"


def test_module_manifest_invalid_version():
    try:
        ModuleManifest(
            name="text", version="invalid",
            capabilities=[], dependencies=[], resource_requirements={},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid version should raise ValueError")


def test_module_manifest_from_dict_roundtrip():
    data = {
        "name": "image",
        "version": "0.0.3.0",
        "capabilities": ["image_generation", "video_generation"],
        "dependencies": ["core_runtime", "gpu_runtime"],
        "resource_requirements": {"min_vram_mb": 6144, "gpu_required": True},
        "description": "Image module",
    }
    manifest = ModuleManifest.from_dict(data)
    assert manifest.name == "image"
    assert manifest.capabilities == ["image_generation", "video_generation"]
    assert manifest.to_dict() == data


def test_module_registry_register_and_get():
    registry = ModuleRegistry()
    manifest = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    assert registry.register_module(manifest) is True
    retrieved = registry.get_module("text")
    assert retrieved is not None
    assert retrieved.name == "text"
    assert retrieved.version == "0.0.3.0"


def test_module_registry_duplicate_without_force():
    registry = ModuleRegistry()
    manifest = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    registry.register_module(manifest)
    same = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    assert registry.register_module(same) is False


def test_module_registry_register_with_force_overwrites():
    registry = ModuleRegistry()
    v1 = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    v2 = ModuleManifest(
        name="text", version="0.0.3.1",
        capabilities=["text_generation", "script_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    registry.register_module(v1)
    assert registry.register_module(v2, force=True) is True
    assert registry.get_module("text").version == "0.0.3.1"


def test_module_registry_unregister():
    registry = ModuleRegistry()
    manifest = ModuleManifest(
        name="audio", version="0.0.3.0",
        capabilities=["speech_synthesis"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    registry.register_module(manifest)
    assert registry.unregister_module("audio") is True
    assert registry.get_module("audio") is None
    assert registry.unregister_module("audio") is False


def test_module_registry_capability_index():
    registry = ModuleRegistry()
    text = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation", "script_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    audio = ModuleManifest(
        name="audio", version="0.0.3.0",
        capabilities=["speech_synthesis", "text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    registry.register_module(text)
    registry.register_module(audio)
    text_modules = registry.get_capabilities("text_generation")
    assert "text" in text_modules
    assert "audio" in text_modules
    assert registry.has_capability("text_generation") is True
    assert registry.has_capability("nonexistent") is False


def test_module_registry_unregister_updates_capability_index():
    registry = ModuleRegistry()
    manifest = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    registry.register_module(manifest)
    assert registry.has_capability("text_generation") is True
    registry.unregister_module("text")
    assert registry.has_capability("text_generation") is False


def test_module_registry_list_modules():
    registry = ModuleRegistry()
    for name in ("text", "image", "audio"):
        manifest = ModuleManifest(
            name=name, version="0.0.3.0",
            capabilities=[f"{name}_generation"],
            dependencies=["core_runtime"],
            resource_requirements={"min_vram_mb": 0},
        )
        registry.register_module(manifest)
    modules = registry.list_modules()
    assert len(modules) == 3
    names = {m.name for m in modules}
    assert names == {"text", "image", "audio"}


def test_module_discovery_report_and_query():
    registry = ModuleRegistry()
    discovery = ModuleDiscovery(registry=registry)
    discovery.report_worker_capabilities(
        "worker-1", ["text_generation", "script_generation"],
        {"vram_mb": 4096, "cpu_cores": 2},
    )
    discovery.report_worker_capabilities(
        "worker-2", ["speech_synthesis"],
        {"vram_mb": 0, "cpu_cores": 1},
    )
    available = discovery.get_available_modules(["text_generation"])
    assert "worker-1" in available["text_generation"]
    assert "worker-2" not in available["text_generation"]
    available_speech = discovery.get_available_modules(["speech_synthesis"])
    assert "worker-2" in available_speech["speech_synthesis"]


def test_module_discovery_serialize_deserialize():
    discovery = ModuleDiscovery()
    discovery.report_worker_capabilities("w1", ["text_generation"], {"vram_mb": 4096})
    data = discovery.serialize_for_transport()
    restored = ModuleDiscovery.deserialize_from_transport(data)
    available = restored.get_available_modules(["text_generation"])
    assert "w1" in available["text_generation"]
