"""Characters, Brands and Channels routes for the Vertep CORE web application."""
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..configuration import (BrandConfig, CharacterConfig, SAFE_ID, load_character,
                            read_json, save_character)
from ..models import Channel, ChannelCreate, ChannelUpdate, CHANNEL_TYPES
from ..state import store

router = APIRouter()


@router.post("/api/characters")
def create_character(config: CharacterConfig):
    if config.id and not SAFE_ID.fullmatch(config.id):
        raise HTTPException(400, "Invalid character ID")
    root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    directory = root / config.id
    if directory.exists():
        raise HTTPException(409, "Character already exists")
    save_character(root, config)
    return config


@router.get("/api/characters")
def characters():
    root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    result = []
    for path in root.glob("*/character.json"):
        try:
            result.append(load_character(root, path.parent.name).model_dump())
        except (OSError, ValueError):
            continue
    return result


@router.get("/api/characters/{character_id}")
def get_character(character_id: str):
    if not SAFE_ID.fullmatch(character_id):
        raise HTTPException(400, "Invalid character ID")
    try:
        return load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(404, f"Character not found or invalid: {error}") from error


@router.put("/api/characters/{character_id}")
def put_character(character_id: str, config: CharacterConfig):
    if character_id != config.id:
        raise HTTPException(400, "Character ID cannot be changed")
    save_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), config)
    return config


@router.delete("/api/characters/{character_id}")
def delete_character(character_id: str):
    if not SAFE_ID.fullmatch(character_id):
        raise HTTPException(400, "Invalid character ID")

    # Collect structured dependencies
    dependent_jobs = [
        {"job_id": job.job_id, "topic": job.topic, "status": job.status.value}
        for job in store.jobs.values()
        if job.character_id == character_id
    ]

    dependent_brands = []
    brand_root = Path(os.getenv("BRANDS_ROOT", "brands"))
    if brand_root.exists():
        for brand_dir in brand_root.iterdir():
            if brand_dir.is_dir():
                char_ref = brand_dir / "character.json"
                if char_ref.exists():
                    try:
                        data = read_json(char_ref)
                        if data.get("character_id") == character_id:
                            dependent_brands.append({"brand_id": brand_dir.name})
                    except Exception:
                        pass

    dependencies = {
        "jobs": dependent_jobs,
        "brands": dependent_brands,
    }

    has_dependencies = bool(dependent_jobs or dependent_brands)

    directory = Path(os.getenv("CHARACTERS_ROOT", "characters")) / character_id
    if not directory.is_dir():
        raise HTTPException(404, "Character not found")

    if has_dependencies:
        raise HTTPException(409, "Character is referenced by other resources", headers={"X-Dependencies": json.dumps(dependencies)})

    shutil.rmtree(directory)
    return {"deleted": character_id}


@router.get("/api/characters/{character_id}/usage")
def character_usage(character_id: str):
    if not SAFE_ID.fullmatch(character_id):
        raise HTTPException(400, "Invalid character ID")

    dependent_jobs = [
        {"job_id": job.job_id, "topic": job.topic, "status": job.status.value, "created_at": job.created_at}
        for job in store.jobs.values()
        if job.character_id == character_id
    ]

    dependent_brands = []
    brand_root = Path(os.getenv("BRANDS_ROOT", "brands"))
    if brand_root.exists():
        for brand_dir in brand_root.iterdir():
            if brand_dir.is_dir():
                char_ref = brand_dir / "character.json"
                if char_ref.exists():
                    try:
                        data = read_json(char_ref)
                        if data.get("character_id") == character_id:
                            dependent_brands.append({"brand_id": brand_dir.name})
                    except Exception:
                        pass

    return {
        "character_id": character_id,
        "jobs": dependent_jobs,
        "brands": dependent_brands,
        "total_jobs": len(dependent_jobs),
        "total_brands": len(dependent_brands),
    }


@router.post("/api/brands")
def create_brand(config: BrandConfig):
    if not config.id:
        raise HTTPException(400, "Brand ID is required")
    if not SAFE_ID.fullmatch(config.id):
        raise HTTPException(400, "Invalid brand ID")
    directory = Path(os.getenv("BRANDS_ROOT", "brands")) / config.id
    if directory.exists():
        raise HTTPException(409, "Brand already exists")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brand.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
    return config


@router.get("/api/brands")
def brands():
    root = Path(os.getenv("BRANDS_ROOT", "brands"))
    result = []
    for path in root.glob("*/brand.json"):
        try:
            result.append(BrandConfig.model_validate(read_json(path)).model_dump())
        except ValueError:
            continue
    return result


@router.put("/api/brands/{brand_id}")
def put_brand(brand_id: str, config: BrandConfig):
    if brand_id != config.id:
        raise HTTPException(400, "Brand ID cannot be changed")
    directory = Path(os.getenv("BRANDS_ROOT", "brands")) / brand_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brand.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
    return config


@router.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: str):
    if any(job.brand_id == brand_id for job in store.jobs.values()):
        raise HTTPException(409, "Бренд використовується у завданнях")
    if not SAFE_ID.fullmatch(brand_id):
        raise HTTPException(400, "Invalid brand ID")
    directory = Path(os.getenv("BRANDS_ROOT", "brands")) / brand_id
    if not directory.is_dir():
        raise HTTPException(404, "Brand not found")
    shutil.rmtree(directory)
    return {"deleted": brand_id}


@router.get("/api/channels/types")
def channel_types():
    return sorted(CHANNEL_TYPES)


@router.get("/api/brands/{brand_id}/channels")
def list_brand_channels(brand_id: str):
    if not SAFE_ID.fullmatch(brand_id):
        raise HTTPException(400, "Invalid brand ID")
    return [ch.model_dump() for ch in store.repository.list_channels(brand_id)]


@router.get("/api/channels/{channel_id}")
def get_channel(channel_id: str):
    channel = store.repository.get_channel(channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return channel.model_dump()


@router.post("/api/brands/{brand_id}/channels")
def create_channel(brand_id: str, config: ChannelCreate):
    if not SAFE_ID.fullmatch(brand_id):
        raise HTTPException(400, "Invalid brand ID")
    if config.brand_id != brand_id:
        raise HTTPException(400, "Brand ID mismatch")
    if config.channel_type not in CHANNEL_TYPES:
        raise HTTPException(400, f"Invalid channel type. Allowed: {', '.join(sorted(CHANNEL_TYPES))}")
    channel = Channel(channel_id=f"ch-{uuid.uuid4().hex[:12]}", brand_id=config.brand_id,
                      channel_type=config.channel_type, target=config.target,
                      enabled=config.enabled, metadata=config.metadata)
    store.repository.save_channel(channel)
    return channel.model_dump()


@router.put("/api/channels/{channel_id}")
def update_channel(channel_id: str, config: ChannelUpdate):
    existing = store.repository.get_channel(channel_id)
    if not existing:
        raise HTTPException(404, "Channel not found")
    if config.target is not None:
        existing.target = config.target
    if config.enabled is not None:
        existing.enabled = config.enabled
    if config.metadata is not None:
        existing.metadata = config.metadata
    store.repository.save_channel(existing)
    return existing.model_dump()


@router.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: str):
    existing = store.repository.get_channel(channel_id)
    if not existing:
        raise HTTPException(404, "Channel not found")
    store.repository.delete_channel(channel_id)
    return {"deleted": channel_id}
