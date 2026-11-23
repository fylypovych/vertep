"""Characters, Brands and Channels routes for the Vertep CORE web application."""
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..configuration import (BrandConfig, CharacterConfig, SAFE_ID, load_character,
                            read_json, save_character)
from ..module.navigation import require_module
from ..models import Channel, ChannelCreate, ChannelUpdate, CHANNEL_TYPES
from ..state import store

router = APIRouter()


@router.post("/api/characters", dependencies=[require_module("characters")])
def create_character(config: CharacterConfig):
    if config.id and not SAFE_ID.fullmatch(config.id):
        raise HTTPException(400, "Invalid character ID")
    root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    directory = root / config.id
    if directory.exists():
        raise HTTPException(409, "Character already exists")
    save_character(root, config)
    return config


@router.get("/api/characters", dependencies=[require_module("characters")])
def characters():
    root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    result = []
    for path in root.glob("*/character.json"):
        try:
            # An incomplete character is not offered as ready (i.0.0.4.76).
            result.append(load_character(root, path.parent.name, require_complete=True).model_dump())
        except (OSError, ValueError):
            continue
    return result


@router.get("/api/characters/{character_id}", dependencies=[require_module("characters")])
def get_character(character_id: str):
    if not SAFE_ID.fullmatch(character_id):
        raise HTTPException(400, "Invalid character ID")
    try:
        return load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(404, f"Character not found or invalid: {error}") from error


def _reference_owner(character_id: str) -> str:
    if not SAFE_ID.fullmatch(character_id):
        raise HTTPException(400, "Invalid character ID")
    if not (Path(os.getenv("CHARACTERS_ROOT", "characters")) / character_id).is_dir():
        raise HTTPException(404, "Character not found")
    return character_id


@router.get("/api/characters/{character_id}/references", dependencies=[require_module("characters")])
def character_references(character_id: str):
    from ..character_references import list_references
    return list_references(_reference_owner(character_id))


@router.post("/api/characters/{character_id}/references", dependencies=[require_module("characters")])
async def upload_character_reference(character_id: str, request: Request):
    from ..character_references import save_reference
    owner = _reference_owner(character_id)
    try:
        return save_reference(owner, await request.body())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/characters/{character_id}/references/{filename}", dependencies=[require_module("characters")])
def character_reference_file(character_id: str, filename: str):
    from ..character_references import reference_path
    path = reference_path(_reference_owner(character_id), filename)
    if path is None:
        raise HTTPException(404, "Reference not found")
    return FileResponse(path)


@router.delete("/api/characters/{character_id}/references/{filename}", dependencies=[require_module("characters")])
def delete_character_reference(character_id: str, filename: str):
    from ..character_references import reference_path
    path = reference_path(_reference_owner(character_id), filename)
    if path is None:
        raise HTTPException(404, "Reference not found")
    path.unlink()
    return {"deleted": filename}


@router.put("/api/characters/{character_id}", dependencies=[require_module("characters")])
def put_character(character_id: str, config: CharacterConfig):
    if character_id != config.id:
        raise HTTPException(400, "Character ID cannot be changed")
    save_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), config)
    return config


@router.delete("/api/characters/{character_id}", dependencies=[require_module("characters")])
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


@router.get("/api/characters/{character_id}/usage", dependencies=[require_module("characters")])
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


def _brand_call(operation, *args):
    """Brands live in the Brands module when it runs, else in the legacy store (#500)."""
    from ..brand_store import BrandStoreError
    try:
        return operation(*args)
    except BrandStoreError as error:
        raise HTTPException(error.status, error.detail) from error


@router.post("/api/brands", dependencies=[require_module("brands")])
def create_brand(config: BrandConfig):
    from ..brand_store import create_brand as store_create
    if not config.id:
        raise HTTPException(400, "Brand ID is required")
    if not SAFE_ID.fullmatch(config.id):
        raise HTTPException(400, "Invalid brand ID")
    return _brand_call(store_create, config.model_dump())


@router.get("/api/brands", dependencies=[require_module("brands")])
def brands():
    from ..brand_store import list_brands
    return _brand_call(list_brands)


@router.post("/api/brands/migrate-to-module", dependencies=[require_module("brands")])
def migrate_brands_to_module():
    """Administrator: copy legacy brands/channels into the running Brands module."""
    from ..brand_store import migrate_to_module
    return _brand_call(migrate_to_module)


@router.put("/api/brands/{brand_id}", dependencies=[require_module("brands")])
def put_brand(brand_id: str, config: BrandConfig):
    from ..brand_store import update_brand
    if brand_id != config.id:
        raise HTTPException(400, "Brand ID cannot be changed")
    return _brand_call(update_brand, brand_id, config.model_dump())


@router.delete("/api/brands/{brand_id}", dependencies=[require_module("brands")])
def delete_brand(brand_id: str):
    from ..brand_store import delete_brand as store_delete
    if any(job.brand_id == brand_id for job in store.jobs.values()):
        raise HTTPException(409, "Бренд використовується у завданнях")
    if not SAFE_ID.fullmatch(brand_id):
        raise HTTPException(400, "Invalid brand ID")
    _brand_call(store_delete, brand_id)
    return {"deleted": brand_id}


@router.get("/api/channels/types", dependencies=[require_module("brands")])
def channel_types():
    return sorted(CHANNEL_TYPES)


@router.get("/api/brands/{brand_id}/channels", dependencies=[require_module("brands")])
def list_brand_channels(brand_id: str):
    if not SAFE_ID.fullmatch(brand_id):
        raise HTTPException(400, "Invalid brand ID")
    from ..brand_store import list_channels
    return [ch.model_dump() for ch in _brand_call(list_channels, brand_id)]


@router.get("/api/channels/{channel_id}", dependencies=[require_module("brands")])
def get_channel(channel_id: str):
    from ..brand_store import get_channel as store_get
    channel = _brand_call(store_get, channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return channel.model_dump()


def _check_channel_target(channel_type: str, target: str) -> None:
    """A brand's Telegram channel is published by the publisher bot (var.0.0.4.86)."""
    from ..publishing_settings import valid_telegram_channel
    if channel_type == "telegram" and not valid_telegram_channel(target):
        raise HTTPException(400, "Канал Telegram: @назва або числовий ідентифікатор")


@router.post("/api/brands/{brand_id}/channels", dependencies=[require_module("brands")])
def create_channel(brand_id: str, config: ChannelCreate):
    if not SAFE_ID.fullmatch(brand_id):
        raise HTTPException(400, "Invalid brand ID")
    if config.brand_id != brand_id:
        raise HTTPException(400, "Brand ID mismatch")
    if config.channel_type not in CHANNEL_TYPES:
        raise HTTPException(400, f"Invalid channel type. Allowed: {', '.join(sorted(CHANNEL_TYPES))}")
    _check_channel_target(config.channel_type, config.target)
    channel = Channel(channel_id=f"ch-{uuid.uuid4().hex[:12]}", brand_id=config.brand_id,
                      channel_type=config.channel_type, target=config.target,
                      enabled=config.enabled, metadata=config.metadata)
    from ..brand_store import create_channel as store_create_channel
    return _brand_call(store_create_channel, channel).model_dump()


@router.put("/api/channels/{channel_id}", dependencies=[require_module("brands")])
def update_channel(channel_id: str, config: ChannelUpdate):
    from ..brand_store import get_channel as store_get, update_channel as store_update
    existing = _brand_call(store_get, channel_id)
    if not existing:
        raise HTTPException(404, "Channel not found")
    if config.target is not None:
        _check_channel_target(existing.channel_type, config.target)
        existing.target = config.target
    if config.enabled is not None:
        existing.enabled = config.enabled
    if config.metadata is not None:
        existing.metadata = config.metadata
    return _brand_call(store_update, existing).model_dump()


@router.delete("/api/channels/{channel_id}", dependencies=[require_module("brands")])
def delete_channel(channel_id: str):
    from ..brand_store import delete_channel as store_delete_channel, get_channel as store_get
    existing = _brand_call(store_get, channel_id)
    if not existing:
        raise HTTPException(404, "Channel not found")
    _brand_call(store_delete_channel, channel_id)
    return {"deleted": channel_id}



@router.get("/api/channels/{channel_id}/credentials", dependencies=[require_module("brands")])
def channel_credentials(channel_id: str):
    """Which credentials a channel publishes with (values are never returned)."""
    from ..brand_store import get_channel as store_get
    from ..channel_secrets import describe
    channel = _brand_call(store_get, channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return describe(channel)


@router.put("/api/channels/{channel_id}/credentials", dependencies=[require_module("brands")])
def put_channel_credentials(channel_id: str, payload: dict, request: Request):
    """Administrator: the channel's own secrets, or the explicit, audited permission to
    use the installation-wide platform credentials for this existing channel."""
    from ..brand_store import get_channel as store_get, update_channel as store_update
    from ..channel_secrets import describe, save
    from ..module.gateway import audit
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        raise HTTPException(403, "Administrator role required")
    channel = _brand_call(store_get, channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    if "secrets" in payload:
        try:
            names = save(channel, payload["secrets"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        audit("channel.credentials", "stored", channel=channel_id, names=names, actor=identity[0])
    if "use_global_credentials" in payload:
        allowed = payload["use_global_credentials"] is True
        channel.metadata = {**(channel.metadata or {}), "use_global_credentials": allowed}
        _brand_call(store_update, channel)
        audit("channel.credentials", "global_allowed" if allowed else "global_revoked", channel=channel_id,
              actor=identity[0])
    return describe(channel)
