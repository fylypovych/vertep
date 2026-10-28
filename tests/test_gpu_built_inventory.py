"""#353: standard pinned image -> entrypoint delivery -> readiness/dispatch.

Only external rendering uses the existing controlled HTTP ComfyUI fixture.
The observed report is read from an actually built image, never reconstructed
from build arguments. No physical GPU/inference acceptance is claimed.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.acceptance_provenance import acceptance_provenance
from tests.test_gpu_http_pipeline import (comfyui, _core_client, _video_job,
    _video_runtime, _post_result, MP4_DECODEABLE)
from tests.test_gpu_runtime_inventory_delivery import _load_script, DOCKERFILE, REPO, _models_root


@pytest.fixture
def built_gpu_image(tmp_path):
    required = os.getenv("VERTEP_REQUIRE_GPU_BUILT_INVENTORY") == "1"
    if not required:
        pytest.skip("Set VERTEP_REQUIRE_GPU_BUILT_INVENTORY=1 for the actual pinned CUDA image build")
    assert shutil.which("docker"), "Docker is required for actual built GPU inventory"
    log = tmp_path / "gpu-build.log"
    image = "vertep-gpu-inventory-" + uuid.uuid4().hex[:12]
    def docker(*args, timeout=120):
        result = subprocess.run(["docker", *map(str, args)], cwd=REPO,
                                capture_output=True, text=True, timeout=timeout)
        assert result.returncode == 0, result.stderr[-6000:]
        return result.stdout
    try:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(["docker", "build", "-f", str(DOCKERFILE), "-t", image, "."],
                                    cwd=REPO, stdout=output, stderr=subprocess.STDOUT, timeout=3600)
        assert result.returncode == 0, log.read_text(encoding="utf-8")[-6000:]
        digest = docker("image", "inspect", "--format", "{{.Id}}", image).strip()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        probe = '''import json,subprocess
report=json.load(open('/opt/comfyui/runtime-inventory.json'))
commits={name:subprocess.check_output(['git','-C',path,'rev-parse','HEAD'],text=True).strip()
 for name,path in [('comfyui','/opt/comfyui'),('ComfyUI-VideoHelperSuite','/opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite')]}
print(json.dumps({'report':report,'checkouts':commits}))'''
        observed = json.loads(docker("run", "--rm", "--entrypoint", "python3", image, "-c", probe))
        for name, component in observed["report"]["components"].items():
            assert component["commit"] == observed["checkouts"][name]
        delivery = tmp_path / "config" / "comfyui-runtime"
        delivery.mkdir(parents=True)
        # --help exits without inference; the unchanged production entrypoint
        # still copies its built report onto the standard shared config mount.
        docker("run", "--rm", "-v", f"{delivery}:/data/runtime-report", image, "--cpu", "--help")
        delivered = json.loads((delivery / "runtime-inventory.json").read_text(encoding="utf-8"))
        assert delivered == observed["report"]
        yield {"image_digest": digest, "observed": observed, "report_root": delivery}
    finally:
        # The uniquely named image is this test's temporary artifact only.
        subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=60)


def test_built_inventory_delivery_readiness_dispatch_and_media_trace(
        built_gpu_image, comfyui, tmp_path, monkeypatch):
    from adapters.comfyui import ComfyUIAdapter
    from adapters.comfyui_runtime import pinned_document
    from worker.service import role_self_test
    from worker.role_executor import execute_role_task
    from core.app import store

    config = tmp_path / "config"
    # The bundle pins and standard deployment generator deliver expected files;
    # observed identities come exclusively from the built image above.
    (config / "comfyui-runtime-pins.json").write_text(
        json.dumps(pinned_document(DOCKERFILE.read_text(encoding="utf-8"))), encoding="utf-8")
    workflows = tmp_path / "storage" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copytree(tmp_path / "image", workflows / "image")
    shutil.copytree(tmp_path / "video", workflows / "video")
    _models_root(tmp_path)
    deployment = _load_script("apply-deployment.py")
    assert deployment.gpu_runtime_inventory(tmp_path) is not None
    expected = config / "comfyui-runtime-inventory.json"
    document = json.loads(expected.read_text(encoding="utf-8"))
    assert document["components"] == built_gpu_image["observed"]["report"]["components"]
    monkeypatch.setenv("WORKFLOWS_ROOT", str(workflows))
    monkeypatch.setenv("MODELS_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("COMFYUI_RUNTIME_INVENTORY", str(expected))
    monkeypatch.setenv("COMFYUI_ROOT", str(built_gpu_image["report_root"]))
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "true")
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    client = _core_client()
    node = "built-inventory-gpu"
    headers = {"x-vertep-token": "worker-token"}

    def readiness():
        return role_self_test("gpu", {"gpu_available": True}, ComfyUIAdapter())
    def heartbeat(result):
        response = client.post("/api/workers/heartbeat", headers=headers, json={
            "node_name": node, "role": "gpu", "status": "FREE", "vram_mb": 32768,
            "capabilities": ["video_generation", "image_generation"], "supported_tasks": ["video", "image"],
            "self_test": result})
        assert response.status_code == 200, response.text
    def claim():
        response = client.post("/api/tasks/claim", headers=headers,
                               json={"node_name": node, "vram_mb": 32768})
        assert response.status_code == 200, response.text
        return response.json().get("task")
    positive = readiness()
    assert positive["status"] == "PASSED", positive
    heartbeat(positive)
    job = _video_job(monkeypatch)
    cases = {}
    original = expected.read_text(encoding="utf-8")
    monkeypatch.setenv("COMFYUI_RUNTIME_INVENTORY", str(config / "missing.json"))
    cases["missing"] = readiness()
    heartbeat(cases["missing"])
    assert cases["missing"]["status"] == "FAILED" and claim() is None
    monkeypatch.setenv("COMFYUI_RUNTIME_INVENTORY", str(expected))
    drift = json.loads(original)
    drift["components"]["comfyui"]["commit"] = "0" * 40
    expected.write_text(json.dumps(drift), encoding="utf-8")
    cases["drift"] = readiness()
    heartbeat(cases["drift"])
    assert cases["drift"]["status"] == "FAILED" and claim() is None
    expected.write_text(original, encoding="utf-8")
    workflow = workflows / "image" / "demo.json"
    original_workflow = workflow.read_bytes()
    workflow.write_bytes(original_workflow + b"\n ")
    cases["checksum"] = readiness()
    heartbeat(cases["checksum"])
    assert cases["checksum"]["status"] == "FAILED" and claim() is None
    workflow.write_bytes(original_workflow)
    recovered = readiness()
    assert recovered["status"] == "PASSED", recovered
    heartbeat(recovered)
    task = claim()
    assert task and task["job_id"] == job.job_id
    _video_runtime(comfyui)
    artifacts = execute_role_task("gpu", task)
    response = _post_result(client, job, task, node_name=node, artifacts=artifacts)
    assert response.status_code == 200, response.text
    saved = store.root / job.job_id / "video" / "scene-001.mp4"
    assert saved.read_bytes() == MP4_DECODEABLE
    sha256 = hashlib.sha256(saved.read_bytes()).hexdigest()
    assert artifacts[0]["contract"]["sha256"] == sha256
    assert job.scenes[0].artifact_ids and job.artifacts[0].kind == "video_scene"
    paths = [path for _, path in comfyui.requests]
    assert "/prompt" in paths and any(p.startswith("/history/") for p in paths)
    assert any(p.startswith("/view") for p in paths)
    (tmp_path / "gpu-built-evidence.json").write_text(json.dumps({
        **built_gpu_image, "report_root": str(built_gpu_image["report_root"]),
        "expected_inventory": document, "negative_readiness": cases,
        "positive": positive, "recovered": recovered,
        "artifact_sha256": sha256, "artifact_ids": job.scenes[0].artifact_ids,
        "http_paths": paths, "physical_gpu_tested": False,
    }, indent=2), encoding="utf-8")
