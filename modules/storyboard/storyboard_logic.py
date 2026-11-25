"""Scenario Library storage and business logic for the Storyboard module (#508).

The Storyboard module owns independent Scenario Documents, their drafts,
ordered scenes, immutable saved versions, inter-brand sharing and access audit
in its own PostgreSQL schema (``mod_storyboard``). Existing Job Storyboards
stay owned by CORE (#501).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from datetime import datetime
from typing import Any, Callable

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
SCENE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ScenarioError(ValueError):
    """Validation error (HTTP 422)."""


class NotFound(LookupError):
    """Scenario or version is missing or hidden from this caller (HTTP 404)."""


class Conflict(RuntimeError):
    """State conflict such as duplicate ID or archived scenario (HTTP 409)."""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return default


def _validate_id(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not SAFE_ID.fullmatch(cleaned):
        raise ScenarioError(f"{label} must match {SAFE_ID.pattern}")
    return cleaned


def _validate_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title or len(title) > 300:
        raise ScenarioError("Title must contain 1-300 characters")
    return title


def _validate_description(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > 5000:
        raise ScenarioError("Description must not exceed 5000 characters")
    return text


def normalize_scenes(raw_scenes: Any, *, require_non_empty: bool = False) -> list[dict[str, Any]]:
    """Validate and normalize ordered scenario scenes."""
    if raw_scenes is None:
        raw_scenes = []
    if not isinstance(raw_scenes, list):
        raise ScenarioError("scenes must be a list")
    if len(raw_scenes) > 200:
        raise ScenarioError("scenes must not exceed 200 items")
    if require_non_empty and not raw_scenes:
        raise ScenarioError("At least one scene is required")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_scenes, 1):
        if not isinstance(item, dict):
            raise ScenarioError(f"Scene #{idx} must be an object")
        prompt = str(item.get("prompt") or item.get("description") or "").strip()
        if not prompt or len(prompt) > 4000:
            raise ScenarioError(f"Scene #{idx} prompt must contain 1-4000 characters")
        video_prompt = str(item.get("video_prompt") or prompt).strip()
        if len(video_prompt) > 4000:
            raise ScenarioError(f"Scene #{idx} video_prompt must not exceed 4000 characters")
        dialogue = str(item.get("dialogue") if item.get("dialogue") is not None else item.get("voiceover") or "").strip()
        voiceover = str(item.get("voiceover") if item.get("voiceover") is not None else dialogue).strip()
        if len(dialogue) > 10000 or len(voiceover) > 10000:
            raise ScenarioError(f"Scene #{idx} dialogue/voiceover must not exceed 10000 characters")
        location = str(item.get("location") or "").strip()
        if len(location) > 500:
            raise ScenarioError(f"Scene #{idx} location must not exceed 500 characters")
        title = str(item.get("title") or "").strip()
        if len(title) > 300:
            raise ScenarioError(f"Scene #{idx} title must not exceed 300 characters")
        try:
            duration = float(item.get("duration", 5.0))
        except (TypeError, ValueError) as error:
            raise ScenarioError(f"Scene #{idx} duration must be a number") from error
        if duration <= 0 or duration > 600:
            raise ScenarioError(f"Scene #{idx} duration must be between 0 and 600 seconds")
        raw_chars = item.get("characters") or []
        if isinstance(raw_chars, str):
            raw_chars = [part.strip() for part in raw_chars.split(",") if part.strip()]
        if not isinstance(raw_chars, list):
            raise ScenarioError(f"Scene #{idx} characters must be a list")
        characters: list[str] = []
        for raw_char in raw_chars:
            cid = _validate_id(str(raw_char), f"Scene #{idx} character")
            if cid not in characters:
                characters.append(cid)
        raw_sid = str(item.get("scene_id") or "").strip()
        if raw_sid and SCENE_ID.fullmatch(raw_sid) and raw_sid not in seen_ids:
            scene_id = raw_sid
        else:
            scene_id = f"scene-{idx:03d}"
            while scene_id in seen_ids:
                scene_id = f"scene-{idx:03d}-{secrets.token_hex(2)}"
        seen_ids.add(scene_id)
        normalized.append({
            "scene_id": scene_id,
            "index": idx,
            "title": title,
            "prompt": prompt,
            "video_prompt": video_prompt,
            "voiceover": voiceover,
            "dialogue": dialogue,
            "location": location,
            "characters": characters,
            "duration": duration,
        })
    return normalized


def snapshot_digest(scenario_id: str, version: int, title: str, description: str, scenes: list[dict]) -> str:
    canonical = json.dumps({
        "scenario_id": scenario_id,
        "version": version,
        "title": title,
        "description": description,
        "scenes": scenes,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def health_check() -> dict[str, Any]:
    return {"ready": True, "module": "storyboard"}


class ScenarioLibraryStore:
    """PostgreSQL store for the Storyboard Scenario Library."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect
        self._lock = threading.RLock()

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                return fn(cursor)
            finally:
                cursor.close()

    def _audit(self, cursor, scenario_id: str, action: str, actor: str,
               brand_id: str | None = None, details: dict | None = None) -> None:
        cursor.execute(
            "INSERT INTO scenario_audit (scenario_id, action, actor, brand_id, details) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (scenario_id, action, actor or "unknown", brand_id,
             json.dumps(details or {}, ensure_ascii=False)),
        )

    def _shares_map(self, cursor, scenario_ids: list[str] | None = None) -> dict[str, list[str]]:
        if scenario_ids is not None:
            if not scenario_ids:
                return {}
            placeholders = ", ".join(["%s"] * len(scenario_ids))
            cursor.execute(
                f"SELECT scenario_id, brand_id FROM scenario_shares "
                f"WHERE scenario_id IN ({placeholders}) ORDER BY brand_id",
                tuple(scenario_ids),
            )
        else:
            cursor.execute("SELECT scenario_id, brand_id FROM scenario_shares ORDER BY scenario_id, brand_id")
        result: dict[str, list[str]] = {}
        for sid, bid in cursor.fetchall():
            result.setdefault(sid, []).append(bid)
        return result

    def _load_row(self, cursor, scenario_id: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT scenario_id, owner_brand_id, title, description, "
            "draft_title, draft_description, draft_scenes, draft_updated_at, draft_updated_by, "
            "latest_version, archived, archived_at, archived_by, "
            "copied_from_scenario_id, copied_from_version, created_at, created_by, updated_at "
            "FROM scenarios WHERE scenario_id = %s",
            (scenario_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    @staticmethod
    def _row_to_dict(row: tuple) -> dict[str, Any]:
        return {
            "scenario_id": row[0],
            "owner_brand_id": row[1],
            "title": row[2],
            "description": row[3],
            "draft_title": row[4],
            "draft_description": row[5],
            "draft_scenes": _json_value(row[6], []),
            "draft_updated_at": _iso(row[7]),
            "draft_updated_by": row[8],
            "latest_version": int(row[9]),
            "archived": bool(row[10]),
            "archived_at": _iso(row[11]),
            "archived_by": row[12],
            "copied_from_scenario_id": row[13],
            "copied_from_version": int(row[14]) if row[14] is not None else None,
            "created_at": _iso(row[15]),
            "created_by": row[16],
            "updated_at": _iso(row[17]),
        }

    @staticmethod
    def _access(row: dict[str, Any], shares: list[str], role: str | None,
                brands: set[str], acting_brand: str | None = None, system: bool = False) -> dict[str, Any] | None:
        owner = row["owner_brand_id"]
        archived = row["archived"]
        has_version = row["latest_version"] > 0
        if system or role == "admin":
            if acting_brand:
                if owner == acting_brand:
                    relation = "own"
                elif acting_brand in shares:
                    relation = "shared"
                else:
                    return None
            else:
                relation = "own"
            is_owner_view = relation == "own"
            return {
                "relation": relation,
                "is_owner": True,
                "can_edit": not archived and is_owner_view,
                "can_manage_access": is_owner_view,
                "can_archive": is_owner_view,
                "can_copy": has_version or is_owner_view,
                "can_create_job": not archived and has_version,
            }
        if role != "moderator":
            return None
        if acting_brand and acting_brand not in brands:
            return None
        effective_brands = {acting_brand} if acting_brand else brands
        if owner in effective_brands:
            return {
                "relation": "own",
                "is_owner": True,
                "can_edit": not archived,
                "can_manage_access": True,
                "can_archive": True,
                "can_copy": True,
                "can_create_job": not archived and has_version,
            }
        if not archived and (set(shares) & effective_brands):
            return {
                "relation": "shared",
                "is_owner": False,
                "can_edit": False,
                "can_manage_access": False,
                "can_archive": False,
                "can_copy": has_version,
                "can_create_job": has_version,
            }
        return None

    @staticmethod
    def _summary(row: dict[str, Any], shares: list[str], access: dict[str, Any],
                 visible_brands: set[str]) -> dict[str, Any]:
        is_owner = access["is_owner"]
        shown_shares = list(shares) if is_owner else sorted(set(shares) & visible_brands)
        return {
            "scenario_id": row["scenario_id"],
            "owner_brand_id": row["owner_brand_id"],
            "title": row["title"],
            "description": row["description"],
            "draft_title": row["draft_title"] if is_owner else row["title"],
            "draft_scene_count": len(row["draft_scenes"]) if is_owner else None,
            "latest_version": row["latest_version"],
            "archived": row["archived"],
            "archived_at": row["archived_at"],
            "shared_brands": shown_shares,
            "relation": access["relation"],
            "can_edit": access["can_edit"],
            "can_manage_access": access["can_manage_access"],
            "can_archive": access["can_archive"],
            "can_copy": access["can_copy"],
            "can_create_job": access["can_create_job"],
            "copied_from_scenario_id": row["copied_from_scenario_id"],
            "copied_from_version": row["copied_from_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # -- catalog -----------------------------------------------------------------

    def list_scenarios(self, role: str | None, brands: set[str], *,
                       brand_id: str | None = None, scope: str = "all",
                       q: str | None = None, archived: str = "false",
                       system: bool = False) -> list[dict[str, Any]]:
        acting = _validate_id(brand_id, "brand_id") if brand_id else None
        if scope not in {"all", "own", "shared"}:
            raise ScenarioError("scope must be all, own, or shared")
        if archived not in {"false", "true", "all"}:
            raise ScenarioError("archived must be false, true, or all")
        needle = (q or "").strip().lower()

        def op(cursor):
            cursor.execute(
                "SELECT scenario_id, owner_brand_id, title, description, "
                "draft_title, draft_description, draft_scenes, draft_updated_at, draft_updated_by, "
                "latest_version, archived, archived_at, archived_by, "
                "copied_from_scenario_id, copied_from_version, created_at, created_by, updated_at "
                "FROM scenarios ORDER BY updated_at DESC, scenario_id ASC"
            )
            rows = [self._row_to_dict(r) for r in cursor.fetchall()]
            shares = self._shares_map(cursor, [r["scenario_id"] for r in rows])
            items = []
            for row in rows:
                row_shares = shares.get(row["scenario_id"], [])
                access = self._access(row, row_shares, role, brands, acting_brand=acting, system=system)
                if access is None:
                    continue
                if archived == "false" and row["archived"]:
                    continue
                if archived == "true" and not row["archived"]:
                    continue
                if scope == "own" and access["relation"] != "own":
                    continue
                if scope == "shared" and access["relation"] != "shared":
                    continue
                if needle:
                    haystack_parts = [
                        row["scenario_id"], row["owner_brand_id"], row["title"], row["description"],
                    ]
                    if access["is_owner"]:
                        haystack_parts.extend([row["draft_title"], row["draft_description"]])
                        for scene in row["draft_scenes"]:
                            haystack_parts.extend([
                                scene.get("title", ""), scene.get("prompt", ""),
                                scene.get("location", ""), scene.get("dialogue", ""),
                                " ".join(scene.get("characters", [])),
                            ])
                    if needle not in " ".join(haystack_parts).lower():
                        continue
                items.append(self._summary(row, row_shares, access, brands))
            return items

        return self._run(op)

    def get_scenario(self, scenario_id: str, role: str | None, brands: set[str], *,
                     brand_id: str | None = None, system: bool = False) -> dict[str, Any]:
        acting = _validate_id(brand_id, "brand_id") if brand_id else None

        def op(cursor):
            row = self._load_row(cursor, scenario_id)
            if row is None:
                raise NotFound("Scenario not found")
            row_shares = self._shares_map(cursor, [scenario_id]).get(scenario_id, [])
            access = self._access(row, row_shares, role, brands, acting_brand=acting, system=system)
            if access is None:
                raise NotFound("Scenario not found")
            cursor.execute(
                "SELECT version, title, description, scenes, note, created_at, created_by "
                "FROM scenario_versions WHERE scenario_id = %s ORDER BY version DESC",
                (scenario_id,),
            )
            versions = []
            for v_row in cursor.fetchall():
                scenes = _json_value(v_row[3], [])
                versions.append({
                    "version": int(v_row[0]),
                    "title": v_row[1],
                    "description": v_row[2],
                    "scenes": scenes,
                    "scene_count": len(scenes),
                    "note": v_row[4],
                    "created_at": _iso(v_row[5]),
                    "created_by": v_row[6],
                    "snapshot_sha256": snapshot_digest(scenario_id, int(v_row[0]), v_row[1], v_row[2], scenes),
                })
            summary = self._summary(row, row_shares, access, brands)
            result = {**summary, "versions": versions}
            if access["is_owner"]:
                result["draft"] = {
                    "title": row["draft_title"],
                    "description": row["draft_description"],
                    "scenes": row["draft_scenes"],
                    "updated_at": row["draft_updated_at"],
                    "updated_by": row["draft_updated_by"],
                }
            else:
                result["draft"] = None
            return result

        return self._run(op)

    # -- create / edit draft / save version --------------------------------------

    def create_scenario(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        owner_brand_id = _validate_id(payload.get("owner_brand_id") or payload.get("brand_id") or "", "owner_brand_id")
        title = _validate_title(payload.get("title"))
        description = _validate_description(payload.get("description"))
        scenes = normalize_scenes(payload.get("scenes"), require_non_empty=False)
        raw_id = payload.get("scenario_id")
        scenario_id = _validate_id(str(raw_id), "scenario_id") if raw_id else f"scn-{secrets.token_hex(6)}"

        def op(cursor):
            if self._load_row(cursor, scenario_id) is not None:
                raise Conflict(f"Scenario {scenario_id} already exists")
            cursor.execute(
                "INSERT INTO scenarios (scenario_id, owner_brand_id, title, description, "
                "draft_title, draft_description, draft_scenes, draft_updated_by, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (scenario_id, owner_brand_id, title, description,
                 title, description, json.dumps(scenes, ensure_ascii=False), actor, actor),
            )
            self._audit(cursor, scenario_id, "create", actor, owner_brand_id,
                        {"title": title, "scene_count": len(scenes)})
            return scenario_id

        created_id = self._run(op)
        return self.get_scenario(created_id, "admin", set(), system=True)

    def _require_owner(self, cursor, scenario_id: str, role: str | None, brands: set[str],
                       *, system: bool = False, allow_archived: bool = False) -> dict[str, Any]:
        row = self._load_row(cursor, scenario_id)
        if row is None:
            raise NotFound("Scenario not found")
        if not (system or role == "admin" or (role == "moderator" and row["owner_brand_id"] in brands)):
            raise PermissionError("Only the owner brand or administrator may modify this scenario")
        if row["archived"] and not allow_archived:
            raise Conflict("Archived scenario cannot be modified until restored")
        return row

    def update_draft(self, scenario_id: str, payload: dict[str, Any], role: str | None,
                     brands: set[str], actor: str, *, system: bool = False) -> dict[str, Any]:
        def op(cursor):
            row = self._require_owner(cursor, scenario_id, role, brands, system=system)
            draft_title = _validate_title(payload["title"]) if "title" in payload else row["draft_title"]
            draft_description = (_validate_description(payload["description"])
                                 if "description" in payload else row["draft_description"])
            draft_scenes = (normalize_scenes(payload["scenes"], require_non_empty=False)
                            if "scenes" in payload else row["draft_scenes"])
            # Keep top-level title/description in sync only while no version is published yet.
            title = draft_title if row["latest_version"] == 0 else row["title"]
            description = draft_description if row["latest_version"] == 0 else row["description"]
            cursor.execute(
                "UPDATE scenarios SET title = %s, description = %s, "
                "draft_title = %s, draft_description = %s, draft_scenes = %s::jsonb, "
                "draft_updated_at = NOW(), draft_updated_by = %s, updated_at = NOW() "
                "WHERE scenario_id = %s",
                (title, description, draft_title, draft_description,
                 json.dumps(draft_scenes, ensure_ascii=False), actor, scenario_id),
            )
            self._audit(cursor, scenario_id, "update_draft", actor, row["owner_brand_id"],
                        {"title": draft_title, "scene_count": len(draft_scenes)})

        self._run(op)
        return self.get_scenario(scenario_id, role, brands, system=system)

    def save_version(self, scenario_id: str, note: str, role: str | None,
                     brands: set[str], actor: str, *, system: bool = False) -> dict[str, Any]:
        clean_note = "" if note is None else str(note).strip()
        if len(clean_note) > 2000:
            raise ScenarioError("Version note must not exceed 2000 characters")

        def op(cursor):
            row = self._require_owner(cursor, scenario_id, role, brands, system=system)
            scenes = normalize_scenes(row["draft_scenes"], require_non_empty=True)
            title = _validate_title(row["draft_title"])
            description = _validate_description(row["draft_description"])
            # Atomically assign the next version number.
            cursor.execute(
                "UPDATE scenarios SET latest_version = latest_version + 1, "
                "title = %s, description = %s, updated_at = NOW() "
                "WHERE scenario_id = %s RETURNING latest_version",
                (title, description, scenario_id),
            )
            new_version = int(cursor.fetchone()[0])
            cursor.execute(
                "INSERT INTO scenario_versions (scenario_id, version, title, description, scenes, note, created_by) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING created_at",
                (scenario_id, new_version, title, description,
                 json.dumps(scenes, ensure_ascii=False), clean_note, actor),
            )
            created_at = _iso(cursor.fetchone()[0])
            self._audit(cursor, scenario_id, "save_version", actor, row["owner_brand_id"],
                        {"version": new_version, "note": clean_note, "scene_count": len(scenes)})
            return {
                "scenario_id": scenario_id,
                "owner_brand_id": row["owner_brand_id"],
                "version": new_version,
                "title": title,
                "description": description,
                "scenes": scenes,
                "note": clean_note,
                "created_at": created_at,
                "created_by": actor,
                "snapshot_sha256": snapshot_digest(scenario_id, new_version, title, description, scenes),
            }

        return self._run(op)

    def list_versions(self, scenario_id: str, role: str | None, brands: set[str], *,
                      brand_id: str | None = None, system: bool = False) -> list[dict[str, Any]]:
        return self.get_scenario(scenario_id, role, brands, brand_id=brand_id, system=system)["versions"]

    def get_version(self, scenario_id: str, version: int, role: str | None, brands: set[str], *,
                    brand_id: str | None = None, system: bool = False) -> dict[str, Any]:
        acting = _validate_id(brand_id, "brand_id") if brand_id else None
        if version < 1:
            raise ScenarioError("version must be >= 1")

        def op(cursor):
            row = self._load_row(cursor, scenario_id)
            if row is None:
                raise NotFound("Scenario not found")
            row_shares = self._shares_map(cursor, [scenario_id]).get(scenario_id, [])
            access = self._access(row, row_shares, role, brands, acting_brand=acting, system=system)
            if access is None:
                raise NotFound("Scenario not found")
            cursor.execute(
                "SELECT version, title, description, scenes, note, created_at, created_by "
                "FROM scenario_versions WHERE scenario_id = %s AND version = %s",
                (scenario_id, version),
            )
            v_row = cursor.fetchone()
            if v_row is None:
                raise NotFound("Scenario version not found")
            scenes = _json_value(v_row[3], [])
            return {
                "scenario_id": scenario_id,
                "owner_brand_id": row["owner_brand_id"],
                "shared_brands": list(row_shares),
                "archived": row["archived"],
                "version": int(v_row[0]),
                "title": v_row[1],
                "description": v_row[2],
                "scenes": scenes,
                "note": v_row[4],
                "created_at": _iso(v_row[5]),
                "created_by": v_row[6],
                "snapshot_sha256": snapshot_digest(scenario_id, int(v_row[0]), v_row[1], v_row[2], scenes),
            }

        return self._run(op)

    # -- copy --------------------------------------------------------------------

    def copy_scenario(self, scenario_id: str, target_brand_id: str, role: str | None,
                      brands: set[str], actor: str, *, version: int | None = None,
                      title: str | None = None, system: bool = False) -> dict[str, Any]:
        target = _validate_id(target_brand_id, "target_brand_id")
        if not (system or role == "admin" or (role == "moderator" and target in brands)):
            raise PermissionError("Brand access required for target brand")

        def op(cursor):
            row = self._load_row(cursor, scenario_id)
            if row is None:
                raise NotFound("Scenario not found")
            row_shares = self._shares_map(cursor, [scenario_id]).get(scenario_id, [])
            access = self._access(row, row_shares, role, brands, system=system)
            if access is None:
                raise NotFound("Scenario not found")
            if not access["is_owner"] and target not in row_shares and not (system or role == "admin"):
                raise PermissionError("Target brand does not have access to this shared scenario")
            chosen_version = version
            if chosen_version is None:
                chosen_version = row["latest_version"] if row["latest_version"] > 0 else None
            if chosen_version is not None:
                cursor.execute(
                    "SELECT title, description, scenes FROM scenario_versions "
                    "WHERE scenario_id = %s AND version = %s",
                    (scenario_id, int(chosen_version)),
                )
                v_row = cursor.fetchone()
                if v_row is None:
                    raise NotFound("Scenario version not found")
                src_title, src_desc, src_scenes = v_row[0], v_row[1], _json_value(v_row[2], [])
            else:
                if not access["is_owner"]:
                    raise Conflict("Shared scenario has no saved versions to copy")
                src_title, src_desc, src_scenes = row["draft_title"], row["draft_description"], row["draft_scenes"]
            new_title = _validate_title(title) if title else f"{src_title} (копія)"
            if len(new_title) > 300:
                new_title = new_title[:300]
            new_id = f"scn-{secrets.token_hex(6)}"
            scenes_json = json.dumps(src_scenes, ensure_ascii=False)
            cursor.execute(
                "INSERT INTO scenarios (scenario_id, owner_brand_id, title, description, "
                "draft_title, draft_description, draft_scenes, draft_updated_by, "
                "latest_version, copied_from_scenario_id, copied_from_version, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
                (new_id, target, new_title, src_desc, new_title, src_desc, scenes_json, actor,
                 1 if src_scenes else 0, scenario_id, chosen_version, actor),
            )
            if src_scenes:
                cursor.execute(
                    "INSERT INTO scenario_versions (scenario_id, version, title, description, scenes, note, created_by) "
                    "VALUES (%s, 1, %s, %s, %s::jsonb, %s, %s)",
                    (new_id, new_title, src_desc, scenes_json,
                     f"Скопійовано з {scenario_id}" + (f" v{chosen_version}" if chosen_version else ""),
                     actor),
                )
            self._audit(cursor, new_id, "copy", actor, target, {
                "copied_from_scenario_id": scenario_id,
                "copied_from_version": chosen_version,
                "source_owner_brand_id": row["owner_brand_id"],
            })
            return new_id

        new_id = self._run(op)
        return self.get_scenario(new_id, role, brands, system=system)

    # -- archive / restore -------------------------------------------------------

    def set_archived(self, scenario_id: str, archived: bool, role: str | None,
                     brands: set[str], actor: str, *, system: bool = False) -> dict[str, Any]:
        def op(cursor):
            row = self._require_owner(cursor, scenario_id, role, brands, system=system, allow_archived=True)
            if archived:
                cursor.execute(
                    "UPDATE scenarios SET archived = TRUE, archived_at = NOW(), archived_by = %s, updated_at = NOW() "
                    "WHERE scenario_id = %s",
                    (actor, scenario_id),
                )
                self._audit(cursor, scenario_id, "archive", actor, row["owner_brand_id"], {})
            else:
                cursor.execute(
                    "UPDATE scenarios SET archived = FALSE, archived_at = NULL, archived_by = NULL, updated_at = NOW() "
                    "WHERE scenario_id = %s",
                    (scenario_id,),
                )
                self._audit(cursor, scenario_id, "restore", actor, row["owner_brand_id"], {})

        self._run(op)
        return self.get_scenario(scenario_id, role, brands, system=system)

    # -- inter-brand sharing & audit ---------------------------------------------

    def list_shares(self, scenario_id: str, role: str | None, brands: set[str],
                    *, system: bool = False) -> list[dict[str, Any]]:
        def op(cursor):
            self._require_owner(cursor, scenario_id, role, brands, system=system, allow_archived=True)
            cursor.execute(
                "SELECT brand_id, granted_at, granted_by FROM scenario_shares "
                "WHERE scenario_id = %s ORDER BY brand_id",
                (scenario_id,),
            )
            return [{"brand_id": r[0], "granted_at": _iso(r[1]), "granted_by": r[2]} for r in cursor.fetchall()]

        return self._run(op)

    def grant_share(self, scenario_id: str, target_brand_id: str, role: str | None,
                    brands: set[str], actor: str, *, system: bool = False) -> list[dict[str, Any]]:
        target = _validate_id(target_brand_id, "brand_id")

        def op(cursor):
            row = self._require_owner(cursor, scenario_id, role, brands, system=system, allow_archived=True)
            if target == row["owner_brand_id"]:
                raise ScenarioError("Owner brand already owns this scenario")
            cursor.execute(
                "INSERT INTO scenario_shares (scenario_id, brand_id, granted_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (scenario_id, brand_id) DO NOTHING",
                (scenario_id, target, actor),
            )
            self._audit(cursor, scenario_id, "share_grant", actor, target,
                        {"owner_brand_id": row["owner_brand_id"], "granted_brand_id": target})

        self._run(op)
        return self.list_shares(scenario_id, role, brands, system=system)

    def revoke_share(self, scenario_id: str, target_brand_id: str, role: str | None,
                     brands: set[str], actor: str, *, system: bool = False) -> list[dict[str, Any]]:
        target = _validate_id(target_brand_id, "brand_id")

        def op(cursor):
            row = self._require_owner(cursor, scenario_id, role, brands, system=system, allow_archived=True)
            cursor.execute(
                "DELETE FROM scenario_shares WHERE scenario_id = %s AND brand_id = %s",
                (scenario_id, target),
            )
            self._audit(cursor, scenario_id, "share_revoke", actor, target,
                        {"owner_brand_id": row["owner_brand_id"], "revoked_brand_id": target})

        self._run(op)
        return self.list_shares(scenario_id, role, brands, system=system)

    def audit_records(self, scenario_id: str, role: str | None, brands: set[str],
                      *, system: bool = False) -> list[dict[str, Any]]:
        def op(cursor):
            self._require_owner(cursor, scenario_id, role, brands, system=system, allow_archived=True)
            cursor.execute(
                "SELECT id, scenario_id, action, actor, brand_id, details, at "
                "FROM scenario_audit WHERE scenario_id = %s ORDER BY id ASC",
                (scenario_id,),
            )
            return [{
                "id": int(r[0]),
                "scenario_id": r[1],
                "action": r[2],
                "actor": r[3],
                "brand_id": r[4],
                "details": _json_value(r[5], {}),
                "at": _iso(r[6]),
            } for r in cursor.fetchall()]

        return self._run(op)
