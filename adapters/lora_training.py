"""Character LoRA training configurator and dataset pipeline for ostris/ai-toolkit (#548).

Enforces a fail-closed allowlist of commercially permitted base checkpoints
(explicitly refusing non-commercial bases such as ``FLUX.1-dev``), prepares
character reference datasets without hardcoding characters, builds ai-toolkit
training configs, and records verifiable ``lora_contract/v1`` artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.character_references import list_references, reference_path

LORA_CONTRACT_FORMAT = "lora_contract/v1"
SAFE_CHAR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_TRIGGER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_ -]{0,63}$")

#: Fail-closed allowlist of commercially permitted base checkpoints (#548).
#: Non-commercial weights (e.g. black-forest-labs/FLUX.1-dev) are strictly excluded.
ALLOWED_LORA_BASE_MODELS: dict[str, dict] = {
    "stabilityai/stable-diffusion-xl-base-1.0": {
        "arch": "sdxl",
        "license": "CreativeML-OpenRAIL++-M",
        "commercial_allowed": True,
        "min_vram_mb": 16384,
        "default_resolution": 1024,
    },
    "black-forest-labs/FLUX.1-schnell": {
        "arch": "flux",
        "license": "Apache-2.0",
        "commercial_allowed": True,
        "min_vram_mb": 24576,
        "default_resolution": 1024,
    },
}


class DisallowedBaseModelError(ValueError):
    """Raised when a requested LoRA base model is absent from the commercial allowlist."""


def validate_base_model(base_model: str) -> dict:
    """Validate that ``base_model`` is in the fail-closed commercial allowlist."""
    cleaned = str(base_model or "").strip()
    if not cleaned:
        raise DisallowedBaseModelError("Base model is required for LoRA training")
    lowered = cleaned.lower()
    if "flux.1-dev" in lowered or "flux-dev" in lowered or "cc-by-nc" in lowered:
        raise DisallowedBaseModelError(
            f"Base model {cleaned!r} has non-commercial weights and is forbidden"
        )
    spec = ALLOWED_LORA_BASE_MODELS.get(cleaned)
    if spec is None or not spec.get("commercial_allowed"):
        allowed = ", ".join(sorted(ALLOWED_LORA_BASE_MODELS))
        raise DisallowedBaseModelError(
            f"Base model {cleaned!r} is not in the commercial allowlist ({allowed})"
        )
    return {"model": cleaned, **spec}


def prepare_character_dataset(
    character_id: str,
    output_dir: Path,
    *,
    trigger_word: str | None = None,
    characters_root: Path | None = None,
) -> dict:
    """Prepare a deterministic training dataset from ``characters/<id>/references``."""
    cid = str(character_id or "").strip()
    if not SAFE_CHAR_ID_RE.fullmatch(cid):
        raise ValueError(f"Invalid character_id: {character_id!r}")
    trigger = (trigger_word or f"vertep_{cid}").strip()
    if not SAFE_TRIGGER_RE.fullmatch(trigger):
        raise ValueError(f"Invalid trigger_word: {trigger!r}")

    previous_root = os.environ.get("CHARACTERS_ROOT")
    if characters_root is not None:
        os.environ["CHARACTERS_ROOT"] = str(characters_root)
    try:
        refs = list_references(cid)
        if not refs:
            raise FileNotFoundError(f"Character {cid!r} has no reference images for LoRA training")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        items: list[dict] = []
        digest = hashlib.sha256()
        for entry in refs:
            src = reference_path(cid, entry["filename"])
            if src is None or not src.is_file():
                continue
            dst = output_dir / src.name
            shutil.copy2(src, dst)
            caption_path = dst.with_suffix(".txt")
            caption_text = f"portrait of {trigger}, consistent character appearance"
            caption_path.write_text(caption_text + "\n", encoding="utf-8")
            raw = dst.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            digest.update(f"{src.name}:{sha}\n".encode("utf-8"))
            items.append({
                "filename": src.name,
                "caption_file": caption_path.name,
                "sha256": sha,
                "size": len(raw),
            })
    finally:
        if characters_root is not None:
            if previous_root is None:
                os.environ.pop("CHARACTERS_ROOT", None)
            else:
                os.environ["CHARACTERS_ROOT"] = previous_root

    if not items:
        raise FileNotFoundError(f"No readable reference images found for character {cid!r}")
    return {
        "character_id": cid,
        "trigger_word": trigger,
        "dataset_dir": str(output_dir),
        "image_count": len(items),
        "images": items,
        "dataset_sha256": digest.hexdigest(),
    }


def build_ai_toolkit_config(
    *,
    character_id: str,
    base_model: str,
    dataset_dir: Path,
    output_dir: Path,
    trigger_word: str | None = None,
    steps: int = 1000,
    rank: int = 16,
    learning_rate: float = 1e-4,
    resolution: int | None = None,
) -> dict:
    """Generate a validated ``ostris/ai-toolkit`` training configuration."""
    cid = str(character_id or "").strip()
    if not SAFE_CHAR_ID_RE.fullmatch(cid):
        raise ValueError(f"Invalid character_id: {character_id!r}")
    spec = validate_base_model(base_model)
    trigger = (trigger_word or f"vertep_{cid}").strip()
    if not SAFE_TRIGGER_RE.fullmatch(trigger):
        raise ValueError(f"Invalid trigger_word: {trigger!r}")
    if not (50 <= int(steps) <= 10000):
        raise ValueError("Training steps must be between 50 and 10000")
    if not (4 <= int(rank) <= 128):
        raise ValueError("LoRA rank must be between 4 and 128")
    if not (1e-6 <= float(learning_rate) <= 1e-2):
        raise ValueError("Learning rate out of safe range (1e-6..1e-2)")
    res = int(resolution or spec["default_resolution"])
    if not (512 <= res <= 1536):
        raise ValueError("Resolution must be between 512 and 1536")

    run_name = f"vertep_{cid}_lora"
    return {
        "job": "extension",
        "config": {
            "name": run_name,
            "process": [
                {
                    "type": "sd_trainer",
                    "training_folder": str(output_dir),
                    "device": "cuda:0",
                    "trigger_word": trigger,
                    "network": {
                        "type": "lora",
                        "linear": int(rank),
                        "linear_alpha": int(rank),
                    },
                    "save": {
                        "dtype": "float16",
                        "save_every": min(250, int(steps)),
                        "max_step_saves_to_keep": 2,
                    },
                    "datasets": [
                        {
                            "folder_path": str(dataset_dir),
                            "caption_ext": "txt",
                            "caption_dropout_rate": 0.05,
                            "resolution": [res],
                        }
                    ],
                    "train": {
                        "batch_size": 1,
                        "steps": int(steps),
                        "gradient_accumulation_steps": 1,
                        "train_unet": True,
                        "train_text_encoder": False,
                        "lr": float(learning_rate),
                        "optimizer": "adamw8bit",
                        "dtype": "bf16",
                    },
                    "model": {
                        "name_or_path": spec["model"],
                        "is_flux": spec["arch"] == "flux",
                        "is_xl": spec["arch"] == "sdxl",
                        "quantize": spec["arch"] == "flux",
                    },
                }
            ],
        },
        "meta": {
            "character_id": cid,
            "base_model": spec["model"],
            "base_model_license": spec["license"],
            "min_vram_mb": spec["min_vram_mb"],
            "trigger_word": trigger,
        },
    }


def save_lora_artifact(
    lora_bytes: bytes,
    *,
    character_id: str,
    base_model: str,
    trigger_word: str,
    steps: int,
    rank: int,
    dataset_sha256: str,
    output_root: Path,
    filename: str | None = None,
) -> dict:
    """Persist a trained LoRA safetensors artifact and its signed metadata."""
    if not lora_bytes:
        raise ValueError("Trained LoRA payload is empty")
    cid = str(character_id or "").strip()
    if not SAFE_CHAR_ID_RE.fullmatch(cid):
        raise ValueError(f"Invalid character_id: {character_id!r}")
    spec = validate_base_model(base_model)
    sha256 = hashlib.sha256(lora_bytes).hexdigest()
    target_dir = Path(output_root) / cid
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = filename or f"{cid}-{sha256[:12]}.safetensors"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.safetensors", safe_name):
        raise ValueError(f"Invalid LoRA filename: {safe_name!r}")

    artifact_path = target_dir / safe_name
    tmp_path = target_dir / f".{safe_name}.tmp"
    tmp_path.write_bytes(lora_bytes)
    tmp_path.replace(artifact_path)

    metadata = {
        "format": LORA_CONTRACT_FORMAT,
        "character_id": cid,
        "filename": safe_name,
        "path": str(artifact_path),
        "comfyui_lora_name": f"{cid}/{safe_name}",
        "base_model": spec["model"],
        "base_model_license": spec["license"],
        "arch": spec["arch"],
        "trigger_word": trigger_word,
        "steps": int(steps),
        "rank": int(rank),
        "dataset_sha256": dataset_sha256,
        "sha256": sha256,
        "size": len(lora_bytes),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = target_dir / f"{safe_name}.meta.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata
