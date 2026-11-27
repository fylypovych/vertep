"""Tests for #542 (i.0.0.5.42): Wan2.2 ComfyUI video workflows (TI2V-5B + 14B, NVIDIA/AMD)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from adapters.comfyui import _substitute_placeholders, placeholder_values
from core.dispatcher import available_worker
from core.models import Job, JobStatus
from core.workflows import WorkflowRegistry, workflow_min_vram_mb

ROOT = Path(__file__).resolve().parents[1]


def test_wan22_workflows_are_valid_and_declare_vram_and_nodes():
    registry = WorkflowRegistry(ROOT / "workflows")
    listed = {item["path"]: item for item in registry.list()}

    wf_5b = listed["workflows/video/wan22-ti2v-5b.json"]
    assert wf_5b["valid"] is True
    assert wf_5b["errors"] == []
    assert wf_5b["min_vram_mb"] == 12288
    assert wf_5b["schema"]["estimated_vram_mb"] == 12288
    assert wf_5b["meta"]["license"] == "Apache-2.0"
    assert set(wf_5b["meta"]["supported_gpu_vendors"]) == {"nvidia", "amd"}
    assert "Wan22ImageToVideoLatent" in wf_5b["schema"]["node_types"]
    assert "VHS_VideoCombine" in wf_5b["schema"]["node_types"]

    wf_14b = listed["workflows/video/wan22-14b.json"]
    assert wf_14b["valid"] is True
    assert wf_14b["errors"] == []
    assert wf_14b["min_vram_mb"] == 24576
    assert wf_14b["schema"]["estimated_vram_mb"] == 24576
    assert wf_14b["meta"]["license"] == "Apache-2.0"
    assert set(wf_14b["meta"]["supported_gpu_vendors"]) == {"nvidia", "amd"}
    assert "EmptyHunyuanLatentVideo" in wf_14b["schema"]["node_types"]
    assert "VHS_VideoCombine" in wf_14b["schema"]["node_types"]

    assert workflow_min_vram_mb("workflows/video/wan22-ti2v-5b.json", ROOT / "workflows") == 12288
    assert workflow_min_vram_mb("workflows/video/wan22-14b.json", ROOT / "workflows") == 24576
    assert workflow_min_vram_mb("workflows/video/sdxl-16x9.json", ROOT / "workflows") == 0


def test_wan22_placeholder_substitution_resolves_all_tokens(monkeypatch):
    monkeypatch.setenv("COMFYUI_WAN_UNET_MODEL", "custom_5b.safetensors")
    monkeypatch.setenv("COMFYUI_WAN_14B_MODEL", "custom_14b.safetensors")
    monkeypatch.setenv("COMFYUI_WAN_CLIP_MODEL", "custom_umt5.safetensors")
    monkeypatch.setenv("COMFYUI_WAN_VAE_MODEL", "custom_wan22_vae.safetensors")
    monkeypatch.setenv("COMFYUI_WAN_14B_VAE_MODEL", "custom_wan21_vae.safetensors")

    for rel, expected_unet, expected_vae in [
        ("workflows/video/wan22-ti2v-5b.json", "custom_5b.safetensors", "custom_wan22_vae.safetensors"),
        ("workflows/video/wan22-14b.json", "custom_14b.safetensors", "custom_wan21_vae.safetensors"),
    ]:
        raw = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        rendered = _substitute_placeholders(raw, placeholder_values("Космічний світанок над Карпатами"))
        dumped = json.dumps(rendered, ensure_ascii=False)
        assert "{{" not in dumped
        assert expected_unet in dumped
        assert expected_vae in dumped
        assert "custom_umt5.safetensors" in dumped
        assert "Космічний світанок над Карпатами" in dumped


def test_dispatcher_enforces_wan22_vram_requirements_for_5b_and_14b():
    now = datetime.now(timezone.utc).isoformat()

    def gpu_worker(name: str, vram_mb: int) -> dict:
        return {
            "node_name": name,
            "role": "gpu",
            "status": "READY",
            "gpu_available": True,
            "vram_mb": vram_mb,
            "free_vram_mb": vram_mb,
            "supported_tasks": ["video", "image"],
            "capabilities": ["video_generation", "image_generation"],
            "tested_capabilities": ["video_generation", "image_generation"],
            "last_seen": now,
            "self_test": {"status": "PASSED", "tested_at": now, "capabilities": ["video_generation", "image_generation"]},
        }

    w8 = gpu_worker("gpu-8g", 8192)
    w16 = gpu_worker("gpu-16g", 16384)
    w24 = gpu_worker("gpu-24g", 24576)

    job_5b = Job(
        job_id="job-wan5b",
        topic="test 5b",
        character_id="c1",
        priority=5,
        status=JobStatus.NEW,
        task_type="video",
        workflow="workflows/video/wan22-ti2v-5b.json",
        min_vram_mb=workflow_min_vram_mb("workflows/video/wan22-ti2v-5b.json", ROOT / "workflows"),
        created_at=now,
    )
    job_14b = Job(
        job_id="job-wan14b",
        topic="test 14b",
        character_id="c1",
        priority=5,
        status=JobStatus.NEW,
        task_type="video",
        workflow="workflows/video/wan22-14b.json",
        min_vram_mb=workflow_min_vram_mb("workflows/video/wan22-14b.json", ROOT / "workflows"),
        created_at=now,
    )

    assert available_worker([w8], job_5b, task_type="video") is None
    assert available_worker([w8, w16], job_5b, task_type="video")["node_name"] == "gpu-16g"
    assert available_worker([w8, w16], job_14b, task_type="video") is None
    assert available_worker([w8, w16, w24], job_14b, task_type="video")["node_name"] == "gpu-24g"


def test_comfyui_nvidia_and_amd_dockerfiles_declare_wan22_node_parity():
    nvidia = (ROOT / "docker" / "comfyui-nvidia" / "Dockerfile").read_text(encoding="utf-8")
    amd = (ROOT / "docker" / "comfyui-amd" / "Dockerfile").read_text(encoding="utf-8")
    for node in (
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "ModelSamplingSD3",
        "Wan22ImageToVideoLatent",
        "EmptyHunyuanLatentVideo",
        "VHS_VideoCombine",
    ):
        assert f'"{node}"' in nvidia, f"Missing {node} in NVIDIA Dockerfile"
        assert f'"{node}"' in amd, f"Missing {node} in AMD Dockerfile"


def test_wan22_14b_runs_both_experts_before_decoding():
    graph = json.loads((ROOT / "workflows/video/wan22-14b.json").read_text())
    low = graph[graph["9"]["inputs"]["samples"][0]]["inputs"]
    high = graph[low["latent_image"][0]]["inputs"]
    assert high["end_at_step"] == low["start_at_step"]
    assert high["return_with_leftover_noise"] == "enable"
    assert low["add_noise"] == "disable"
    assert low["end_at_step"] == low["steps"]
    models = [
        graph[graph[inputs["model"][0]]["inputs"]["model"][0]]["inputs"]["unet_name"]
        for inputs in (high, low)
    ]
    assert models == ["{{WAN_14B_MODEL}}", "{{WAN_14B_LOW_NOISE_MODEL}}"]
