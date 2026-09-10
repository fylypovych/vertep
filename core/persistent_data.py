"""Persistent user-data helpers for issue 32."""
from __future__ import annotations
import json, os, shutil
from pathlib import Path

def _data_storage() -> Path:
    return Path(os.getenv("JOB_ROOT", "/data/storage/jobs")).resolve().parent

def persistent_characters_root() -> Path:
    return Path(os.getenv("CHARACTERS_ROOT", str(_data_storage() / "characters")))

def persistent_brands_root() -> Path:
    return Path(os.getenv("BRANDS_ROOT", str(_data_storage() / "brands")))

def workflows_persistent_root() -> Path:
    return Path(os.getenv("WORKFLOWS_ROOT", str(_data_storage() / "workflows")))

def _copy_missing(src: Path, dst: Path, overwrite: bool = False) -> int:
    if not src.is_dir():
        return 0
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
            try:
                shutil.copy2(item, tmp)
                tmp.replace(target)
                copied += 1
            finally:
                if tmp.exists():
                    try: tmp.unlink()
                    except OSError: pass
        elif item.is_dir():
            (dst / item.relative_to(src)).mkdir(parents=True, exist_ok=True)
    return copied

def ensure_persistent_user_data(*, seed_overwrite: bool = False) -> dict:
    c_root = persistent_characters_root()
    b_root = persistent_brands_root()
    w_root = workflows_persistent_root()
    for p in (c_root, b_root, w_root):
        p.mkdir(parents=True, exist_ok=True)
    marker = c_root.parent / ".seed-initialized"
    migrated: dict[str,int] = {}
    # migrate legacy image-layer path /app/<name> only (not ./<name> on dev host — it contains demo seed that would pollute prod storage)
    for label, dst in (("characters", c_root), ("brands", b_root), ("workflows", w_root)):
        for cand in [Path(f"/app/{label}")]:
            try:
                if cand.resolve() == dst.resolve():
                    continue
            except OSError:
                pass
            if cand.is_dir():
                migrated[label] = migrated.get(label, 0) + _copy_missing(cand, dst, overwrite=False)
    should_seed = not marker.exists()
    seeded: dict[str,int] = {}
    if should_seed:
        any_content = any(any(p.is_file() for p in r.rglob("*")) for r in (c_root, b_root, w_root))
        if any_content and any(migrated.values()):
            should_seed = False
            try: marker.write_text(json.dumps({"seed":"skipped-migrated"})+"\n", encoding="utf-8")
            except OSError: pass
    if should_seed:
        for label, dst in (("characters", c_root), ("brands", b_root), ("workflows", w_root)):
            seed_env = {"characters":"CHARACTERS_SEED_ROOT","brands":"BRANDS_SEED_ROOT","workflows":"WORKFLOWS_SEED_ROOT"}[label]
            cand = os.getenv(seed_env)
            sources = [Path(cand)] if cand else [Path(f"/app/{label}"), Path(label)]
            for src in sources:
                try:
                    if src.resolve() == dst.resolve():
                        continue
                except OSError:
                    pass
                if src.is_dir():
                    seeded[label] = seeded.get(label, 0) + _copy_missing(src, dst, overwrite=seed_overwrite)
        try: marker.write_text(json.dumps({"seeded":seeded,"migrated":migrated})+"\n", encoding="utf-8")
        except OSError: pass
    return {"characters_root":str(c_root),"brands_root":str(b_root),"workflows_root":str(w_root),"seeded":seeded,"migrated":migrated,"seed_skipped": not should_seed}
