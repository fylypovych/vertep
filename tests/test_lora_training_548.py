"""Tests for #548 (i.0.0.5.48): Character LoRA training pipeline via ostris/ai-toolkit."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.comfyui import _substitute_placeholders, placeholder_values
from adapters.lora_training import (
    ALLOWED_LORA_BASE_MODELS,
    DisallowedBaseModelError,
    LORA_CONTRACT_FORMAT,
    build_ai_toolkit_config,
    prepare_character_dataset,
    validate_base_model,
)
from core.character_references import save_reference
from core.dispatcher import available_worker
from core.models import Job, JobStatus
from core.outbound_only import inbound_port_violations
from services import lora_trainer_service
from worker.role_executor import execute_role_task

ROOT = Path(__file__).resolve().parents[1]
PNG_SAMPLE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_lora_base_model_allowlist_blocks_flux_dev_and_unknown_models():
    assert "stabilityai/stable-diffusion-xl-base-1.0" in ALLOWED_LORA_BASE_MODELS
    assert "black-forest-labs/FLUX.1-schnell" in ALLOWED_LORA_BASE_MODELS

    sdxl = validate_base_model("stabilityai/stable-diffusion-xl-base-1.0")
    assert sdxl["arch"] == "sdxl" and sdxl["commercial_allowed"] is True
    schnell = validate_base_model("black-forest-labs/FLUX.1-schnell")
    assert schnell["arch"] == "flux" and schnell["license"] == "Apache-2.0"

    for bad in ("black-forest-labs/FLUX.1-dev", "org/custom-flux-dev", "unknown/checkpoint", ""):
        with pytest.raises(DisallowedBaseModelError):
            validate_base_model(bad)


def test_prepare_character_dataset_and_build_ai_toolkit_config(monkeypatch, tmp_path):
    chars_root = tmp_path / "characters"
    monkeypatch.setenv("CHARACTERS_ROOT", str(chars_root))
    save_reference("hero_01", PNG_SAMPLE)

    dataset = prepare_character_dataset("hero_01", tmp_path / "ds", trigger_word="hero_v1")
    assert dataset["image_count"] == 1
    assert len(dataset["dataset_sha256"]) == 64
    img_entry = dataset["images"][0]
    assert (tmp_path / "ds" / img_entry["filename"]).is_file()
    assert "hero_v1" in (tmp_path / "ds" / img_entry["caption_file"]).read_text(encoding="utf-8")

    cfg = build_ai_toolkit_config(
        character_id="hero_01",
        base_model="black-forest-labs/FLUX.1-schnell",
        dataset_dir=tmp_path / "ds",
        output_dir=tmp_path / "out",
        trigger_word="hero_v1",
        steps=500,
        rank=16,
    )
    assert cfg["job"] == "extension"
    proc = cfg["config"]["process"][0]
    assert proc["type"] == "sd_trainer"
    assert proc["model"]["name_or_path"] == "black-forest-labs/FLUX.1-schnell"
    assert proc["model"]["is_flux"] is True
    assert proc["network"]["linear"] == 16
    assert cfg["meta"]["min_vram_mb"] == 24576


def test_lora_trainer_service_and_worker_execution(monkeypatch, tmp_path):
    chars_root = tmp_path / "characters"
    lora_root = tmp_path / "loras"
    monkeypatch.setenv("CHARACTERS_ROOT", str(chars_root))
    monkeypatch.setenv("LORA_MODELS_ROOT", str(lora_root))
    save_reference("char_alpha", PNG_SAMPLE)

    fake_entry = tmp_path / "ai-toolkit" / "run.py"
    fake_entry.parent.mkdir(parents=True)
    fake_entry.write_text("# stub", encoding="utf-8")
    monkeypatch.setenv("AI_TOOLKIT_ROOT", str(fake_entry.parent))

    fake_lora_bytes = b"SAFETENSORS_LORA_WEIGHTS_" + b"\xab" * 128

    def fake_run(cmd, **kwargs):
        cfg = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        out_dir = Path(cfg["config"]["process"][0]["training_folder"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "vertep_char_alpha_lora.safetensors").write_bytes(fake_lora_bytes)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(lora_trainer_service.subprocess, "run", fake_run)
    client = TestClient(lora_trainer_service.app)

    assert client.get("/health").status_code == 200

    # Non-commercial FLUX.1-dev rejected fail-closed with 422
    rejected = client.post(
        "/train",
        json={
            "character_id": "char_alpha",
            "base_model": "black-forest-labs/FLUX.1-dev",
            "images": [{"filename": "ref.png", "content_base64": base64.b64encode(PNG_SAMPLE).decode("ascii")}],
        },
    )
    assert rejected.status_code == 422

    # End-to-end GPU Worker execution via execute_role_task
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: client.post("/train", json=json))
    monkeypatch.setenv("LORA_TRAINER_URL", "http://lora-trainer:8101")
    task = {
        "task": "lora_train",
        "character_id": "char_alpha",
        "base_model": "stabilityai/stable-diffusion-xl-base-1.0",
        "trigger_word": "alpha_person",
        "steps": 250,
        "rank": 16,
    }
    artifacts = execute_role_task("gpu", task, _migration=True)
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["kind"] == "lora"
    assert art["contract"]["format"] == LORA_CONTRACT_FORMAT
    assert art["contract"]["sha256"] == hashlib.sha256(fake_lora_bytes).hexdigest()
    assert art["contract"]["comfyui_lora_name"].startswith("char_alpha/")
    assert (lora_root / "char_alpha" / art["filename"]).is_file()
    assert (lora_root / "char_alpha" / f"{art['filename']}.meta.json").is_file()

    # ComfyUI {{LORA_MODEL}} placeholder substitution works without character hardcode
    monkeypatch.setenv("COMFYUI_LORA_MODEL", art["contract"]["comfyui_lora_name"])
    substituted = _substitute_placeholders({"1": {"inputs": {"lora_name": "{{LORA_MODEL}}"}}}, placeholder_values("t"))
    assert substituted["1"]["inputs"]["lora_name"] == art["contract"]["comfyui_lora_name"]

    # Unauthorized non-GPU role refused
    with pytest.raises(PermissionError):
        execute_role_task("voice", task, _migration=True)


def test_lora_dispatcher_capability_and_outbound_only_compose():
    now = datetime.now(timezone.utc).isoformat()
    job = Job(
        job_id="job-lora-1",
        topic="train lora",
        character_id="c1",
        priority=5,
        status=JobStatus.NEW,
        task_type="lora_train",
        min_vram_mb=24576,
        created_at=now,
    )
    gpu_no_lora = {
        "node_name": "gpu-gen",
        "role": "gpu",
        "status": "READY",
        "vram_mb": 24576,
        "free_vram_mb": 24576,
        "capabilities": ["image_generation"],
        "tested_capabilities": ["image_generation"],
        "supported_tasks": ["image"],
        "last_seen": now,
        "self_test": {"status": "PASSED", "tested_at": now, "capabilities": ["image_generation"]},
    }
    gpu_lora = {
        **gpu_no_lora,
        "node_name": "gpu-lora",
        "capabilities": ["image_generation", "lora_training"],
        "tested_capabilities": ["image_generation", "lora_training"],
        "supported_tasks": ["image", "lora_train"],
        "self_test": {"status": "PASSED", "tested_at": now, "capabilities": ["image_generation", "lora_training"]},
    }
    assert available_worker([gpu_no_lora], job, task_type="lora_train") is None
    assert available_worker([gpu_no_lora, gpu_lora], job, task_type="lora_train")["node_name"] == "gpu-lora"

    dockerfile = (ROOT / "docker" / "lora-trainer" / "Dockerfile").read_text(encoding="utf-8")
    assert "ostris/ai-toolkit" in dockerfile
    assert "services.lora_trainer_service:app" in dockerfile
    assert inbound_port_violations(ROOT / "deploy" / "docker-compose.yml") == []
