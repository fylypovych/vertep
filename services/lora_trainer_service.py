"""Локальний GPU HTTP runtime для навчання LoRA на базі ostris/ai-toolkit (MIT, #548)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from adapters.lora_training import (
    ALLOWED_LORA_BASE_MODELS,
    DisallowedBaseModelError,
    LORA_CONTRACT_FORMAT,
    build_ai_toolkit_config,
    validate_base_model,
)

app = FastAPI(title="Vertep LoRA Trainer (ai-toolkit)", version="1")


def _ai_toolkit_entry() -> Path | None:
    root = Path(os.getenv("AI_TOOLKIT_ROOT", "/opt/ai-toolkit"))
    entry = root / "run.py"
    return entry if entry.is_file() else None


class TrainImage(BaseModel):
    filename: str = Field(min_length=1, max_length=128)
    content_base64: str = Field(min_length=1)
    caption: str | None = Field(default=None, max_length=500)


class LoraTrainRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    base_model: str = Field(min_length=1, max_length=128)
    trigger_word: str | None = Field(default=None, max_length=64)
    steps: int = Field(default=1000, ge=50, le=10000)
    rank: int = Field(default=16, ge=4, le=128)
    learning_rate: float = Field(default=1e-4, ge=1e-6, le=1e-2)
    resolution: int = Field(default=1024, ge=512, le=1536)
    images: list[TrainImage] = Field(min_length=1, max_length=32)


@app.get("/health")
def health() -> dict:
    entry = _ai_toolkit_entry()
    if entry is None:
        raise HTTPException(503, "ostris/ai-toolkit runtime недоступний")
    return {
        "status": "HEALTHY",
        "engine": "ai-toolkit",
        "allowed_base_models": sorted(ALLOWED_LORA_BASE_MODELS),
    }


@app.post("/train")
def train_lora(request: LoraTrainRequest) -> dict:
    try:
        spec = validate_base_model(request.base_model)
    except DisallowedBaseModelError as error:
        raise HTTPException(422, str(error)) from error

    entry = _ai_toolkit_entry()
    if entry is None:
        raise HTTPException(503, "ostris/ai-toolkit runtime недоступний")

    trigger = (request.trigger_word or f"vertep_{request.character_id}").strip()
    with tempfile.TemporaryDirectory(prefix="vertep-lora-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        dataset_dir = tmp_root / "dataset"
        output_dir = tmp_root / "output"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        ds_digest = hashlib.sha256()
        for img in request.images:
            name = Path(img.filename).name
            if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise HTTPException(422, f"Непідтримуваний формат референсу: {name}")
            try:
                raw = base64.b64decode(img.content_base64, validate=True)
            except Exception as error:
                raise HTTPException(422, f"Некоректний base64 для {name}") from error
            if not raw:
                raise HTTPException(422, f"Порожній файл референсу: {name}")
            (dataset_dir / name).write_bytes(raw)
            caption = (img.caption or f"portrait of {trigger}").strip()
            (dataset_dir / name).with_suffix(".txt").write_text(caption + "\n", encoding="utf-8")
            ds_digest.update(f"{name}:{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))

        try:
            config = build_ai_toolkit_config(
                character_id=request.character_id,
                base_model=request.base_model,
                dataset_dir=dataset_dir,
                output_dir=output_dir,
                trigger_word=trigger,
                steps=request.steps,
                rank=request.rank,
                learning_rate=request.learning_rate,
                resolution=request.resolution,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

        config_path = tmp_root / "job.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

        python_bin = os.getenv("AI_TOOLKIT_PYTHON", "python3")
        try:
            subprocess.run(
                [python_bin, str(entry), str(config_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=int(os.getenv("LORA_TRAIN_TIMEOUT_SECONDS", "14400")),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            raise HTTPException(502, "ai-toolkit не зміг завершити навчання LoRA") from error

        weights = sorted(output_dir.rglob("*.safetensors"))
        if not weights:
            raise HTTPException(502, "ai-toolkit завершився без .safetensors артефакту")
        lora_bytes = weights[-1].read_bytes()
        if not lora_bytes:
            raise HTTPException(502, "Отримано порожній файл LoRA")
        sha256 = hashlib.sha256(lora_bytes).hexdigest()

    return {
        "format": LORA_CONTRACT_FORMAT,
        "engine": "ai-toolkit",
        "character_id": request.character_id,
        "base_model": spec["model"],
        "base_model_license": spec["license"],
        "trigger_word": trigger,
        "steps": request.steps,
        "rank": request.rank,
        "dataset_sha256": ds_digest.hexdigest(),
        "filename": f"{request.character_id}-{sha256[:12]}.safetensors",
        "sha256": sha256,
        "size": len(lora_bytes),
        "lora_base64": base64.b64encode(lora_bytes).decode("ascii"),
    }
