"""Contract-level regressions for signed modules and controlled GPU calls."""

import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.module.packages import ModulePackages
from core.module.manifest import ModuleManifest
from core.module.registry import ModuleRegistry
from core.module.discovery import ModuleDiscovery
from core.gpu.provider_registry import GPUProviderRegistry
from core.gpu.router import GPURouter


def signed_package(key):
    content = {"manifest": {"name": "example", "version": "1.0.0", "capabilities": ["text"],
                            "dependencies": [{"name": "core_runtime", "min_version": "0.0.2", "max_version": "0.1.0"}]},
               "files": {"module.py": base64.b64encode(b"def execute(value): return value['text'].upper()\ndef health(): return {'ready': True}\ndef resource_query(): return {'cpu_cores': 1}\ndef cancel(task_id): return {'cancelled': False}\n").decode()}}
    signature = key.sign(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    return {**content, "signer": "owner", "signature": base64.b64encode(signature).decode()}


def test_signed_packages_cli_and_tamper(tmp_path):
    key = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust"
    trust.mkdir()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    (trust / "owner.pem").write_bytes(public)
    package = signed_package(key)
    source = tmp_path / "package.json"
    source.write_text(json.dumps(package), encoding="utf-8")
    env = {**os.environ, "VERTEP_MODULE_ROOT": str(tmp_path / "installed"), "VERTEP_MODULE_TRUST_ROOT": str(trust)}

    def cli(*args):
        return subprocess.run([sys.executable, "scripts/vertep-cli.py", "module", *args], env=env, capture_output=True, text=True, check=True)

    assert json.loads(cli("install", str(source)).stdout)["version"] == "1.0.0"
    assert json.loads(cli("info", "example").stdout)["name"] == "example"
    assert len(json.loads(cli("list").stdout)) == 1
    platform_command = (["pwsh", "-NoProfile", "-File", "scripts/vertep.ps1", "module", "list"] if os.name == "nt"
                        else ["bash", "scripts/vertep", "module", "list"])
    platform_result = subprocess.run(platform_command, env=env, capture_output=True, text=True, check=True)
    assert json.loads(platform_result.stdout)[0]["name"] == "example"
    from worker.module_runtime import execute_module, inventory
    saved = {key: os.environ.get(key) for key in ("VERTEP_MODULE_ROOT", "VERTEP_MODULE_TRUST_ROOT")}
    try:
        os.environ.update({key: env[key] for key in saved})
        assert inventory()[0]["name"] == "example"
        assert execute_module("example", {"text": "actual module output", "module_version": "1.0.0"}) == "ACTUAL MODULE OUTPUT"
        with pytest.raises(ValueError, match="version"):
            execute_module("example", {"text": "x", "module_version": "2.0.0"})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    cli("uninstall", "example")
    assert json.loads(cli("list").stdout) == []
    assert (tmp_path / "installed/example.json").exists()
    tampered = deepcopy(package)
    tampered["files"]["module.py"] = base64.b64encode(b"raise RuntimeError('tampered')").decode()
    manager = ModulePackages(tmp_path / "other", {"owner": public})
    with pytest.raises(Exception):
        manager.install(tampered)
    assert manager.list() == []
    with pytest.raises(KeyError):
        ModulePackages(tmp_path / "untrusted", {}).install(package)


def test_dependency_range_and_capability_replacement():
    registry = ModuleRegistry({"core_runtime": "0.0.2.87"})
    manifest = ModuleManifest.from_dict({"manifest": {"name": "x", "version": "1.0.0", "capabilities": ["old"],
        "dependencies": [{"name": "core_runtime", "min_version": "0.0.2", "max_version": "0.1.0"}]}})
    assert registry.register_module(manifest)
    replacement = ModuleManifest("x", "1.0.1", ["new"], [], {})
    assert registry.register_module(replacement, force=True)
    assert not registry.has_capability("old")
    assert registry.has_capability("new")
    bad = ModuleManifest("bad", "1.0.0", [], [{"name": "x", "min_version": "2.0.0"}], {})
    assert not registry.register_module(bad)
    discovery = ModuleDiscovery(registry)
    discovery.report_worker_capabilities("worker", ["old"], {})
    discovery.report_worker_capabilities("worker", ["new"], {})
    assert discovery.get_available_modules(["old", "new"]) == {"old": [], "new": ["worker"]}


class ControlledProvider:
    capabilities = {"text"}
    min_vram_mb = 0
    max_concurrent_tasks = 1

    def __init__(self, name, result=None, local=True):
        self.name, self.result, self.is_local = name, result, local
        self.calls = []

    def query_capability(self, request):
        return {"available": True}

    def get_resource_usage(self):
        return {"vram_total_mb": 0, "vram_used_mb": 0}

    def submit_inference(self, task):
        self.calls.append(task)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result or {"status": "complete", "result_ref": "controlled-output"}

    def cancel_inference(self, task_id):
        return {"cancelled": True, "cleanup": True}


def test_gpu_configured_priority_fallback_and_local_only():
    registry = GPUProviderRegistry()
    cloud = ControlledProvider("cloud", local=False)
    fallback = ControlledProvider("fallback")
    primary = ControlledProvider("primary", {"status": "failed", "cleanup": True})
    outside = ControlledProvider("outside")
    for name, provider, priority in [("fallback", fallback, 10), ("outside", outside, 0), ("cloud", cloud, 1), ("primary", primary, 2)]:
        registry.register_provider(name, provider, priority)
    router = GPURouter(registry, allowed=["cloud", "primary", "fallback"], local_only=True)
    assert router.execute("text", {"task_id": "x"})["result_ref"] == "controlled-output"
    assert len(primary.calls) == len(fallback.calls) == 1
    assert not cloud.calls and not outside.calls


def test_gpu_uncertain_submit_blocks_duplicate_and_capacity():
    registry = GPUProviderRegistry()
    primary = ControlledProvider("primary", TimeoutError())
    fallback = ControlledProvider("fallback")
    registry.register_provider("primary", primary, 1)
    registry.register_provider("fallback", fallback, 2)
    router = GPURouter(registry)
    with pytest.raises(RuntimeError, match="uncertain"):
        router.execute("text", {"task_id": "x"})
    with pytest.raises(RuntimeError, match="already submitted"):
        router.execute("text", {"task_id": "x"})
    assert not fallback.calls
    assert router.cancel("x")
    with pytest.raises(RuntimeError, match="uncertain"):
        router.execute("text", {"task_id": "attempt-1", "inference_operation_id": "same-stage"})
    with pytest.raises(RuntimeError, match="already submitted"):
        router.execute("text", {"task_id": "attempt-2", "inference_operation_id": "same-stage"})
    assert len(primary.calls) == 2
    assert not fallback.calls
