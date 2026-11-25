"""Characters Application Module logic (#499).

Business logic for the Characters module: ownership, collaboration, activation,
profile completeness, and reference images. Runs inside the module's container.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pg8000.dbapi

CORE = os.environ["VERTEP_CORE_URL"].rstrip("/")
TOKEN = os.environ["VERTEP_MODULE_TOKEN"]
UI_DIR = Path(__file__).with_name("ui")
#: The ID names the character's directory: the same rule as CORE (core/configuration.SAFE_ID).
CHARACTER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_DB_LOCK = threading.Lock()


class InvalidCharacterId(ValueError):
    """A request error (422), not a conflict with an existing character (409)."""

_DB: dict[str, Any] = {"connection": None}

REQUIRED_PROFILE_FILES = (
    "character.json",
    "system_prompt.txt",
    "voice.json",
    "visual.json",
    "generation.json",
    "publishing.json",
)


def _core(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        CORE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-vertep-module-token": TOKEN, "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def database() -> pg8000.dbapi.Connection | None:
    """Connection to the module's own schema; None until CORE has provisioned it."""
    with _DB_LOCK:
        if _DB["connection"] is None:
            try:
                credentials = _core("GET", "/api/modules/self/database")
            except (urllib.error.URLError, OSError, ValueError):
                return None
            connection = pg8000.dbapi.connect(
                user=credentials["role"],
                password=credentials["password"],
                host=credentials["host"],
                port=credentials["port"],
                database=credentials["database"],
            )
            connection.autocommit = True
            _DB["connection"] = connection
        return _DB["connection"]


def query(sql: str, args: tuple = ()) -> list:
    connection = database()
    if connection is None:
        raise RuntimeError("module database is not provisioned")
    with _DB_LOCK:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, args)
            return cursor.fetchall() if cursor.description else []
        except Exception:
            _DB["connection"] = None
            raise


def execute(sql: str, args: tuple = ()) -> None:
    connection = database()
    if connection is None:
        raise RuntimeError("module database is not provisioned")
    with _DB_LOCK:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, args)
        except Exception:
            _DB["connection"] = None
            raise


def _verify_token(request_headers: dict) -> bool:
    return hmac.compare_digest(request_headers.get("x-vertep-module-token", ""), TOKEN)


def _character_profile_complete(character_id: str, characters_root: Path) -> tuple[bool, list[str]]:
    """Check if character has all required profile files."""
    directory = characters_root / character_id
    missing = [name for name in REQUIRED_PROFILE_FILES if not (directory / name).is_file()]
    if not missing:
        try:
            _load_character_profile(character_id, characters_root)
        except (OSError, ValueError) as error:
            return False, [str(error)]
    return len(missing) == 0, missing


def _load_character_profile(character_id: str, characters_root: Path) -> dict | None:
    """Load character profile from files."""
    directory = characters_root / character_id
    if not (directory / "character.json").is_file():
        return None
    import json as json_lib
    base = json_lib.loads((directory / "character.json").read_text(encoding="utf-8"))
    if not isinstance(base, dict):
        raise ValueError("character.json must contain an object")
    for name in ("system_prompt", "voice", "visual", "generation", "publishing"):
        path = directory / f"{name}.txt" if name == "system_prompt" else directory / f"{name}.json"
        if path.is_file():
            if name == "system_prompt":
                base[name] = path.read_text(encoding="utf-8")
            else:
                base[name] = json_lib.loads(path.read_text(encoding="utf-8"))
                if not isinstance(base[name], dict):
                    raise ValueError(f"{path.name} must contain an object")
        else:
            base[name] = {} if name != "system_prompt" else ""
    return base


class CharactersModule:
    """Characters Application Module business logic."""

    def __init__(self):
        self.characters_root = Path(os.getenv("CHARACTERS_ROOT", "/data/characters"))
        self.characters_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database tables."""
        connection = database()
        if connection is None:
            return
        with _DB_LOCK:
            cursor = connection.cursor()
            # Characters table with ownership
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'uk',
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    workflow TEXT,
                    owner_brand_id TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Character profile files stored as JSONB
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS character_profiles (
                    character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
                    system_prompt TEXT,
                    voice JSONB,
                    visual JSONB,
                    generation JSONB,
                    publishing JSONB,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # References (images)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS character_references (
                    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    PRIMARY KEY (character_id, filename)
                )
            """)
            # Collaborations: which brands can use which characters
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS character_collaborations (
                    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    brand_id TEXT NOT NULL,
                    granted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    granted_by TEXT NOT NULL,
                    PRIMARY KEY (character_id, brand_id)
                )
            """)
            # Audit log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS character_audit (
                    id BIGSERIAL PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_brand_id TEXT,
                    details JSONB,
                    at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)

    def _audit(self, character_id: str, action: str, actor_brand_id: str | None, details: dict | None = None) -> None:
        execute(
            "INSERT INTO character_audit (character_id, action, actor_brand_id, details) VALUES (%s, %s, %s, %s)",
            (character_id, action, actor_brand_id, json.dumps(details) if details else None),
        )

    def list_characters(self, brand_id: str | None = None, include_inactive: bool = False) -> list[dict]:
        """List characters visible to the brand.

        Per AC3: Active characters are available to all brands without allowlist.
        Owner sees their characters + all active characters from other owners.
        """
        if brand_id:
            # Owner's characters + all active characters (AC3: no allowlist)
            rows = query("""
                SELECT c.id, c.name, c.language, c.enabled, c.workflow, c.owner_brand_id, c.active, c.created_at, c.updated_at
                FROM characters c
                WHERE c.owner_brand_id = %s OR c.active = TRUE
                ORDER BY c.owner_brand_id = %s DESC, c.created_at DESC
            """, (brand_id, brand_id))
        else:
            rows = query("SELECT id, name, language, enabled, workflow, owner_brand_id, active, created_at, updated_at FROM characters ORDER BY created_at DESC")
        result = []
        for row in rows:
            char_id = row[0]
            profile = query("SELECT system_prompt, voice, visual, generation, publishing FROM character_profiles WHERE character_id = %s", (char_id,))
            refs = query("SELECT filename, sha256, size FROM character_references WHERE character_id = %s", (char_id,))
            p = profile[0] if profile else (None, {}, {}, {}, {})
            result.append({
                "id": char_id,
                "name": row[1],
                "language": row[2],
                "enabled": row[3],
                "workflow": row[4],
                "owner_brand_id": row[5],
                "active": row[6],
                "created_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
                "system_prompt": p[0] or "",
                "voice": p[1] or {},
                "visual": p[2] or {},
                "generation": p[3] or {},
                "publishing": p[4] or {},
                "references": [{"filename": r[0], "sha256": r[1], "size": r[2]} for r in refs],
                "is_owner": brand_id is not None and row[5] == brand_id,
            })
        return result

    def get_character(self, character_id: str, brand_id: str | None = None) -> dict | None:
        """Get single character with full profile.

        Per AC3: Active characters are visible to all brands.
        Inactive characters are visible only to owner.
        """
        row = query("""
            SELECT id, name, language, enabled, workflow, owner_brand_id, active, created_at, updated_at
            FROM characters WHERE id = %s
        """, (character_id,))
        if not row:
            return None
        row = row[0]
        # Check access: owner or active character (AC3)
        if brand_id and row[5] != brand_id and not row[6]:
            return None
        profile = query("SELECT system_prompt, voice, visual, generation, publishing FROM character_profiles WHERE character_id = %s", (character_id,))
        refs = query("SELECT filename, sha256, size FROM character_references WHERE character_id = %s", (character_id,))
        p = profile[0] if profile else (None, {}, {}, {}, {})
        return {
            "id": row[0],
            "name": row[1],
            "language": row[2],
            "enabled": row[3],
            "workflow": row[4],
            "owner_brand_id": row[5],
            "active": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "updated_at": row[8].isoformat() if row[8] else None,
            "system_prompt": p[0] or "",
            "voice": p[1] or {},
            "visual": p[2] or {},
            "generation": p[3] or {},
            "publishing": p[4] or {},
            "references": [{"filename": r[0], "sha256": r[1], "size": r[2]} for r in refs],
            "is_owner": brand_id is not None and row[5] == brand_id,
        }

    def create_character(self, brand_id: str, data: dict) -> dict:
        """Create a new character owned by brand_id."""
        character_id = data.get("id") or f"character_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]}"
        if not isinstance(character_id, str) or not CHARACTER_ID.fullmatch(character_id):
            raise InvalidCharacterId("Invalid character ID")
        # Check if exists
        if query("SELECT 1 FROM characters WHERE id = %s", (character_id,)):
            raise ValueError("Character already exists")
        execute("""
            INSERT INTO characters (id, name, language, enabled, workflow, owner_brand_id, active)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE)
        """, (character_id, data["name"], data.get("language", "uk"), data.get("enabled", True),
              data.get("workflow"), brand_id))
        # Save profile
        execute("""
            INSERT INTO character_profiles (character_id, system_prompt, voice, visual, generation, publishing)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (character_id, data.get("system_prompt", ""), json.dumps(data.get("voice", {})),
              json.dumps(data.get("visual", {})), json.dumps(data.get("generation", {})),
              json.dumps(data.get("publishing", {}))))
        self._write_character_files(character_id, data, creating=True)
        self._audit(character_id, "create", brand_id, {"name": data["name"]})
        return self.get_character(character_id, brand_id) or {"id": character_id}

    def _write_character_files(self, character_id: str, data: dict, *, creating: bool = False) -> None:
        directory = self.characters_root / character_id
        directory.mkdir(parents=True, exist_ok=True)
        current = {}
        character_path = directory / "character.json"
        if not creating and character_path.is_file():
            current = json.loads(character_path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                raise ValueError("character.json must contain an object")
        character = {
            "id": character_id,
            "name": data.get("name", current.get("name", "")),
            "language": data.get("language", current.get("language", "uk")),
            "enabled": data.get("enabled", current.get("enabled", True)),
            "workflow": data.get("workflow", current.get("workflow")),
        }
        character_path.write_text(json.dumps(character, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ("system_prompt", "voice", "visual", "generation", "publishing"):
            if name not in data:
                continue
            value = data[name]
            path = directory / ("system_prompt.txt" if name == "system_prompt" else f"{name}.json")
            if name == "system_prompt":
                path.write_text(str(value), encoding="utf-8")
            else:
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_character(self, character_id: str, brand_id: str, data: dict) -> dict:
        """Update character (owner only)."""
        char = self.get_character(character_id, brand_id)
        if not char or char["owner_brand_id"] != brand_id:
            raise ValueError("Not owner or character not found")
        execute("""
            UPDATE characters SET name = %s, language = %s, enabled = %s, workflow = %s, updated_at = NOW()
            WHERE id = %s
        """, (data.get("name", char["name"]), data.get("language", char["language"]),
              data.get("enabled", char["enabled"]), data.get("workflow", char.get("workflow")), character_id))
        execute("""
            UPDATE character_profiles SET system_prompt = %s, voice = %s, visual = %s, generation = %s, publishing = %s, updated_at = NOW()
            WHERE character_id = %s
        """, (data.get("system_prompt", char["system_prompt"]), json.dumps(data.get("voice", char["voice"])),
              json.dumps(data.get("visual", char["visual"])), json.dumps(data.get("generation", char["generation"])),
              json.dumps(data.get("publishing", char["publishing"])), character_id))
        self._write_character_files(character_id, data)
        self._audit(character_id, "update", brand_id, data)
        return self.get_character(character_id, brand_id) or {}

    def delete_character(self, character_id: str, brand_id: str) -> None:
        """Delete character (owner only, no dependencies)."""
        char = self.get_character(character_id, brand_id)
        if not char or char["owner_brand_id"] != brand_id:
            raise ValueError("Not owner or character not found")
        # Check for active jobs using this character
        jobs = _core("GET", f"/api/characters/{character_id}/usage")
        if jobs.get("total_jobs", 0) > 0 or jobs.get("total_brands", 0) > 0:
            raise ValueError("Character is referenced by other resources")
        execute("DELETE FROM characters WHERE id = %s", (character_id,))
        self._audit(character_id, "delete", brand_id, {})

    def activate_character(self, character_id: str, brand_id: str) -> dict:
        """Activate character for collaboration (owner/admin only)."""
        char = self.get_character(character_id, brand_id)
        if not char or char["owner_brand_id"] != brand_id:
            raise ValueError("Not owner or character not found")
        # Check profile completeness
        complete, missing = _character_profile_complete(character_id, self.characters_root)
        if not complete:
            raise ValueError(f"Character profile incomplete: missing {', '.join(missing)}")
        execute("UPDATE characters SET active = TRUE, updated_at = NOW() WHERE id = %s", (character_id,))
        self._audit(character_id, "activate", brand_id, {})
        return self.get_character(character_id, brand_id) or {}

    def deactivate_character(self, character_id: str, brand_id: str) -> dict:
        """Deactivate character (blocks new collaborations, keeps references/materials).

        Per AC4: Deactivation blocks new collaborations but preserves references/materials.
        Already running generation completes, already created pending approval material can be approved/published.
        Does not automatically revoke approvals.
        """
        char = self.get_character(character_id, brand_id)
        if not char or char["owner_brand_id"] != brand_id:
            raise ValueError("Not owner or character not found")
        execute("UPDATE characters SET active = FALSE, updated_at = NOW() WHERE id = %s", (character_id,))
        self._audit(character_id, "deactivate", brand_id, {})
        return self.get_character(character_id, brand_id) or {}

    def upload_reference(self, character_id: str, brand_id: str, data: bytes) -> dict:
        """Upload reference image (owner only)."""
        char = self.get_character(character_id, brand_id)
        if not char or char["owner_brand_id"] != brand_id:
            raise ValueError("Not owner or character not found")
        # Validate image
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("Image exceeds 10 MB")
        suffix = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ValueError("Only PNG, JPEG, WebP supported")
        digest = hashlib.sha256(data).hexdigest()
        filename = f"{digest[:16]}{suffix}"
        refs = query("SELECT filename FROM character_references WHERE character_id = %s", (character_id,))
        if filename not in [r[0] for r in refs] and len(refs) >= 5:
            raise ValueError("Maximum 5 references allowed")
        # Save file
        ref_dir = self.characters_root / character_id / "references"
        ref_dir.mkdir(parents=True, exist_ok=True)
        (ref_dir / filename).write_bytes(data)
        execute("""
            INSERT INTO character_references (character_id, filename, sha256, size)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (character_id, filename) DO UPDATE SET sha256 = EXCLUDED.sha256, size = EXCLUDED.size
        """, (character_id, filename, digest, len(data)))
        self._audit(character_id, "reference_upload", brand_id, {"filename": filename})
        return {"filename": filename, "sha256": digest, "size": len(data)}

    def delete_reference(self, character_id: str, brand_id: str, filename: str) -> None:
        """Delete reference image (owner only)."""
        char = self.get_character(character_id, brand_id)
        if not char or char["owner_brand_id"] != brand_id:
            raise ValueError("Not owner or character not found")
        path = self.characters_root / character_id / "references" / filename
        if path.is_file():
            path.unlink()
        execute("DELETE FROM character_references WHERE character_id = %s AND filename = %s", (character_id, filename))
        self._audit(character_id, "reference_delete", brand_id, {"filename": filename})

    def check_generation_allowed(self, character_id: str, brand_id: str | None = None) -> tuple[bool, list[str]]:
        """Whether a NEW generation of ``brand_id`` may use the character.

        The owner brand always may (complete profile); another brand only while
        the owner keeps the character active for collaboration (AC2/AC3).
        Running generations and pending material are never re-checked (AC4).
        """
        char = query("SELECT active, owner_brand_id FROM characters WHERE id = %s", (character_id,))
        if not char:
            return False, ["Character not found"]
        complete, missing = _character_profile_complete(character_id, self.characters_root)
        if not complete:
            return False, [f"Incomplete profile: missing {', '.join(missing)}"]
        active, owner = char[0]
        self.last_owner = owner
        if brand_id is not None and brand_id == owner:
            return True, []
        if not active:
            return False, ["Character is not active"]
        return True, []


def migrate_existing_characters(module: CharactersModule, legacy_root: Path, default_owner_brand: str | None = None) -> dict:
    """Migrate existing characters from filesystem to module database.

    Scans the legacy CHARACTERS_ROOT for complete character directories and imports them
    into the module's database.

    Per AC6: Ambiguous owner must not be assigned silently. If default_owner_brand is None,
    migration requires explicit owner mapping and skips characters without clear ownership.
    Preserves IDs/profiles/references/history and existing jobs.
    """
    results = {"migrated": [], "skipped": [], "errors": []}

    if not legacy_root.is_dir():
        return results

    if not isinstance(default_owner_brand, str) or not default_owner_brand.strip():
        # AC6: No silent default owner assignment
        results["skipped"].append({
            "id": "*",
            "reason": "default_owner_brand not provided. AC6 requires explicit owner mapping for migration."
        })
        return results
    default_owner_brand = default_owner_brand.strip()

    for char_dir in legacy_root.iterdir():
        if not char_dir.is_dir():
            continue
        character_id = char_dir.name
        if character_id == "references":
            continue

        # Check completeness without importing CORE code into the isolated module container.
        missing = [name for name in REQUIRED_PROFILE_FILES if not (char_dir / name).is_file()]
        if missing:
            results["skipped"].append({"id": character_id, "reason": f"incomplete: missing {', '.join(missing)}"})
            continue

        # Check if already exists in module database
        existing = query("SELECT 1 FROM characters WHERE id = %s", (character_id,))
        if existing:
            results["skipped"].append({"id": character_id, "reason": "already exists in module database"})
            continue

        try:
            # Load character from filesystem
            config = _load_character_profile(character_id, legacy_root)
            if config is None or config.get("id", character_id) != character_id:
                raise ValueError("character.json ID does not match its directory")
            if not isinstance(config.get("name"), str) or not config["name"].strip():
                raise ValueError("character.json must contain a character name")
            if not isinstance(config.get("system_prompt"), str):
                raise ValueError("system_prompt.txt must contain text")
            module_dir = module.characters_root / character_id
            same_directory = module_dir.resolve() == char_dir.resolve()
            if not same_directory:
                for filename in REQUIRED_PROFILE_FILES:
                    target = module_dir / filename
                    source_data = (char_dir / filename).read_bytes()
                    if target.is_file() and target.read_bytes() != source_data:
                        raise ValueError(f"Module profile file already differs: {filename}")
            legacy_refs = char_dir / "references"
            module_refs = module_dir / "references"
            if legacy_refs.is_dir() and not same_directory:
                for ref_file in legacy_refs.iterdir():
                    target = module_refs / ref_file.name
                    if ref_file.is_file() and target.is_file() and target.read_bytes() != ref_file.read_bytes():
                        raise ValueError(f"Module reference already differs: {ref_file.name}")

            # Insert into module database
            execute("""
                INSERT INTO characters (id, name, language, enabled, workflow, owner_brand_id, active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
            """, (character_id, config["name"], config.get("language", "uk"), config.get("enabled", True),
                  config.get("workflow"), default_owner_brand))

            # Insert profile
            execute("""
                INSERT INTO character_profiles (character_id, system_prompt, voice, visual, generation, publishing, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """, (character_id, config["system_prompt"], json.dumps(config["voice"]), json.dumps(config["visual"]),
                  json.dumps(config["generation"]), json.dumps(config["publishing"])))

            if not same_directory:
                module_dir.mkdir(parents=True, exist_ok=True)
                for filename in REQUIRED_PROFILE_FILES:
                    target = module_dir / filename
                    if not target.exists():
                        shutil.copy2(char_dir / filename, target)

            # Copy reference images
            if legacy_refs.is_dir():
                module_refs.mkdir(parents=True, exist_ok=True)
                for ref_file in legacy_refs.iterdir():
                    if ref_file.is_file():
                        target = module_refs / ref_file.name
                        if not target.exists():
                            shutil.copy2(ref_file, target)
                        # Record in database
                        data = ref_file.read_bytes()
                        digest = hashlib.sha256(data).hexdigest()
                        execute("""
                            INSERT INTO character_references (character_id, filename, sha256, size, uploaded_at)
                            VALUES (%s, %s, %s, %s, NOW())
                            ON CONFLICT (character_id, filename) DO NOTHING
                        """, (character_id, ref_file.name, digest, ref_file.stat().st_size))

            # Audit log
            execute("""
                INSERT INTO character_audit (character_id, action, actor_brand_id, details, at)
                VALUES (%s, 'migrate', %s, %s, NOW())
            """, (character_id, default_owner_brand, json.dumps({"source": "legacy_filesystem"})))

            results["migrated"].append(character_id)

        except Exception as e:
            results["errors"].append({"id": character_id, "error": str(e)})

    return results


def health_check() -> dict:
    return {"ready": database() is not None}