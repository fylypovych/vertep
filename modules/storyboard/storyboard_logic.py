import os
import json
import logging
import pg8000.native
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@dataclass
class Script:
    id: str
    brand_id: str
    title: str
    description: str
    status: str  # draft, ready, generating, completed, failed
    script_data: Dict[str, Any]  # Structure: acts, scenes, characters, etc.
    owner_user_id: str
    active: bool
    created_at: str
    updated_at: str


@dataclass
class Scene:
    id: str
    script_id: str
    scene_number: int
    title: str
    description: str
    scene_data: Dict[str, Any]  # characters, location, duration, dialogue, visual_prompts, etc.
    status: str  # pending, generating, completed, failed
    generated_assets: List[Dict[str, Any]]
    created_at: str
    updated_at: str


@dataclass
class Storyboard:
    id: str
    script_id: str
    name: str
    panels: List[Dict[str, Any]]  # Ordered panels with image_ref, description, timing, etc.
    owner_user_id: str
    active: bool
    created_at: str
    updated_at: str


@dataclass
class StoryboardCollaboration:
    id: int
    entity_type: str  # script, scene, storyboard
    entity_id: str
    collaborator_user_id: str
    permission_level: str  # read, write, admin
    active: bool
    created_at: str
    updated_at: str


class StoryboardModule:
    def __init__(self, db_url: str):
        self.db_url = db_url

    @contextmanager
    def _conn(self):
        conn = pg8000.native.Connection(self.db_url)
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_script(self, row) -> Script:
        return Script(
            id=row[0],
            brand_id=row[1],
            title=row[2],
            description=row[3] or "",
            status=row[4],
            script_data=json.loads(row[5]) if row[5] else {},
            owner_user_id=row[6],
            active=row[7],
            created_at=str(row[8]),
            updated_at=str(row[9]),
        )

    def _row_to_scene(self, row) -> Scene:
        return Scene(
            id=row[0],
            script_id=row[1],
            scene_number=row[2],
            title=row[3],
            description=row[4] or "",
            scene_data=json.loads(row[5]) if row[5] else {},
            status=row[6],
            generated_assets=json.loads(row[7]) if row[7] else [],
            created_at=str(row[8]),
            updated_at=str(row[9]),
        )

    def _row_to_storyboard(self, row) -> Storyboard:
        return Storyboard(
            id=row[0],
            script_id=row[1],
            name=row[2],
            panels=json.loads(row[3]) if row[3] else [],
            owner_user_id=row[4],
            active=row[5],
            created_at=str(row[6]),
            updated_at=str(row[7]),
        )

    def _row_to_collab(self, row) -> StoryboardCollaboration:
        return StoryboardCollaboration(
            id=row[0],
            entity_type=row[1],
            entity_id=row[2],
            collaborator_user_id=row[3],
            permission_level=row[4],
            active=row[5],
            created_at=str(row[6]),
            updated_at=str(row[7]),
        )

    # Script CRUD
    def create_script(self, script: Script) -> Script:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO scripts (id, brand_id, title, description, status, script_data, owner_user_id, active, created_at, updated_at)
                VALUES (:id, :brand_id, :title, :description, :status, :script_data, :owner_user_id, :active, :created_at, :updated_at)
                """,
                id=script.id,
                brand_id=script.brand_id,
                title=script.title,
                description=script.description,
                status=script.status,
                script_data=json.dumps(script.script_data),
                owner_user_id=script.owner_user_id,
                active=script.active,
                created_at=script.created_at,
                updated_at=script.updated_at,
            )
            self._audit(conn, "script", script.id, "create", script.owner_user_id, asdict(script))
            return script

    def get_script(self, script_id: str) -> Optional[Script]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM scripts WHERE id = :id", id=script_id)
            return self._row_to_script(rows[0]) if rows else None

    def list_scripts(self, brand_id: Optional[str] = None, owner_user_id: Optional[str] = None, active_only: bool = False) -> List[Script]:
        with self._conn() as conn:
            sql = "SELECT * FROM scripts"
            params = {}
            conditions = []
            if brand_id:
                conditions.append("brand_id = :brand_id")
                params["brand_id"] = brand_id
            if owner_user_id:
                conditions.append("owner_user_id = :owner_user_id")
                params["owner_user_id"] = owner_user_id
            if active_only:
                conditions.append("active = true")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_script(r) for r in rows]

    def update_script(self, script_id: str, updates: Dict[str, Any], actor_user_id: str) -> Optional[Script]:
        with self._conn() as conn:
            script = self.get_script(script_id)
            if not script:
                return None
            if script.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can update script")

            allowed = {"title", "description", "status", "script_data", "active", "brand_id"}
            set_clauses = []
            params = {"id": script_id, "updated_at": datetime.utcnow().isoformat()}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k in ("script_data",) else v
            if not set_clauses:
                return script
            set_clauses.append("updated_at = :updated_at")
            conn.run(f"UPDATE scripts SET {', '.join(set_clauses)} WHERE id = :id", **params)
            self._audit(conn, "script", script_id, "update", actor_user_id, updates)
            return self.get_script(script_id)

    def delete_script(self, script_id: str, actor_user_id: str) -> bool:
        with self._conn() as conn:
            script = self.get_script(script_id)
            if not script:
                return False
            if script.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can delete script")
            conn.run("DELETE FROM scripts WHERE id = :id", id=script_id)
            self._audit(conn, "script", script_id, "delete", actor_user_id, {})
            return True

    def activate_script(self, script_id: str, actor_user_id: str) -> Optional[Script]:
        return self.update_script(script_id, {"active": True}, actor_user_id)

    def deactivate_script(self, script_id: str, actor_user_id: str) -> Optional[Script]:
        return self.update_script(script_id, {"active": False}, actor_user_id)

    # Scene CRUD
    def create_scene(self, scene: Scene) -> Scene:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO scenes (id, script_id, scene_number, title, description, scene_data, status, generated_assets, created_at, updated_at)
                VALUES (:id, :script_id, :scene_number, :title, :description, :scene_data, :status, :generated_assets, :created_at, :updated_at)
                """,
                id=scene.id,
                script_id=scene.script_id,
                scene_number=scene.scene_number,
                title=scene.title,
                description=scene.description,
                scene_data=json.dumps(scene.scene_data),
                status=scene.status,
                generated_assets=json.dumps(scene.generated_assets),
                created_at=scene.created_at,
                updated_at=scene.updated_at,
            )
            self._audit(conn, "scene", scene.id, "create", scene.owner_user_id if hasattr(scene, 'owner_user_id') else "system", asdict(scene))
            return scene

    def get_scene(self, scene_id: str) -> Optional[Scene]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM scenes WHERE id = :id", id=scene_id)
            return self._row_to_scene(rows[0]) if rows else None

    def list_scenes(self, script_id: str) -> List[Scene]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM scenes WHERE script_id = :script_id ORDER BY scene_number", script_id=script_id)
            return [self._row_to_scene(r) for r in rows]

    def update_scene(self, scene_id: str, updates: Dict[str, Any], actor_user_id: str) -> Optional[Scene]:
        with self._conn() as conn:
            scene = self.get_scene(scene_id)
            if not scene:
                return None
            script = self.get_script(scene.script_id)
            if script and script.owner_user_id != actor_user_id:
                raise PermissionError("Only script owner can update scene")

            allowed = {"title", "description", "scene_data", "status", "generated_assets"}
            set_clauses = []
            params = {"id": scene_id, "updated_at": datetime.utcnow().isoformat()}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k in ("scene_data", "generated_assets") else v
            if not set_clauses:
                return scene
            set_clauses.append("updated_at = :updated_at")
            conn.run(f"UPDATE scenes SET {', '.join(set_clauses)} WHERE id = :id", **params)
            self._audit(conn, "scene", scene_id, "update", actor_user_id, updates)
            return self.get_scene(scene_id)

    def delete_scene(self, scene_id: str, actor_user_id: str) -> bool:
        with self._conn() as conn:
            scene = self.get_scene(scene_id)
            if not scene:
                return False
            script = self.get_script(scene.script_id)
            if script and script.owner_user_id != actor_user_id:
                raise PermissionError("Only script owner can delete scene")
            conn.run("DELETE FROM scenes WHERE id = :id", id=scene_id)
            self._audit(conn, "scene", scene_id, "delete", actor_user_id, {})
            return True

    # Storyboard CRUD
    def create_storyboard(self, storyboard: Storyboard) -> Storyboard:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO storyboards (id, script_id, name, panels, owner_user_id, active, created_at, updated_at)
                VALUES (:id, :script_id, :name, :panels, :owner_user_id, :active, :created_at, :updated_at)
                """,
                id=storyboard.id,
                script_id=storyboard.script_id,
                name=storyboard.name,
                panels=json.dumps(storyboard.panels),
                owner_user_id=storyboard.owner_user_id,
                active=storyboard.active,
                created_at=storyboard.created_at,
                updated_at=storyboard.updated_at,
            )
            self._audit(conn, "storyboard", storyboard.id, "create", storyboard.owner_user_id, asdict(storyboard))
            return storyboard

    def get_storyboard(self, storyboard_id: str) -> Optional[Storyboard]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM storyboards WHERE id = :id", id=storyboard_id)
            return self._row_to_storyboard(rows[0]) if rows else None

    def list_storyboards(self, script_id: Optional[str] = None, owner_user_id: Optional[str] = None, active_only: bool = False) -> List[Storyboard]:
        with self._conn() as conn:
            sql = "SELECT * FROM storyboards"
            params = {}
            conditions = []
            if script_id:
                conditions.append("script_id = :script_id")
                params["script_id"] = script_id
            if owner_user_id:
                conditions.append("owner_user_id = :owner_user_id")
                params["owner_user_id"] = owner_user_id
            if active_only:
                conditions.append("active = true")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_storyboard(r) for r in rows]

    def update_storyboard(self, storyboard_id: str, updates: Dict[str, Any], actor_user_id: str) -> Optional[Storyboard]:
        with self._conn() as conn:
            sb = self.get_storyboard(storyboard_id)
            if not sb:
                return None
            if sb.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can update storyboard")

            allowed = {"name", "panels", "active"}
            set_clauses = []
            params = {"id": storyboard_id, "updated_at": datetime.utcnow().isoformat()}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k == "panels" else v
            if not set_clauses:
                return sb
            set_clauses.append("updated_at = :updated_at")
            conn.run(f"UPDATE storyboards SET {', '.join(set_clauses)} WHERE id = :id", **params)
            self._audit(conn, "storyboard", storyboard_id, "update", actor_user_id, updates)
            return self.get_storyboard(storyboard_id)

    def delete_storyboard(self, storyboard_id: str, actor_user_id: str) -> bool:
        with self._conn() as conn:
            sb = self.get_storyboard(storyboard_id)
            if not sb:
                return False
            if sb.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can delete storyboard")
            conn.run("DELETE FROM storyboards WHERE id = :id", id=storyboard_id)
            self._audit(conn, "storyboard", storyboard_id, "delete", actor_user_id, {})
            return True

    # Collaborations
    def add_collaboration(self, entity_type: str, entity_id: str, collaborator_user_id: str, permission_level: str, actor_user_id: str) -> StoryboardCollaboration:
        with self._conn() as conn:
            if entity_type not in ("script", "scene", "storyboard"):
                raise ValueError("Invalid entity type")
            if permission_level not in ("read", "write", "admin"):
                raise ValueError("Invalid permission level")
            # Check ownership
            if entity_type == "script":
                entity = self.get_script(entity_id)
            elif entity_type == "scene":
                entity = self.get_scene(entity_id)
                if entity:
                    entity = self.get_script(entity.script_id)
            else:
                entity = self.get_storyboard(entity_id)
            if not entity:
                raise ValueError("Entity not found")
            if entity.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can add collaborations")
            if hasattr(entity, 'active') and not entity.active:
                raise ValueError("Cannot add collaboration to inactive entity")
            now = datetime.utcnow().isoformat()
            conn.run(
                """
                INSERT INTO storyboard_collaborations (entity_type, entity_id, collaborator_user_id, permission_level, active, created_at, updated_at)
                VALUES (:entity_type, :entity_id, :collaborator_user_id, :permission_level, true, :created_at, :updated_at)
                RETURNING id
                """,
                entity_type=entity_type,
                entity_id=entity_id,
                collaborator_user_id=collaborator_user_id,
                permission_level=permission_level,
                created_at=now,
                updated_at=now,
            )
            row = conn.run("SELECT * FROM storyboard_collaborations WHERE entity_type = :entity_type AND entity_id = :entity_id AND collaborator_user_id = :collaborator_user_id ORDER BY created_at DESC LIMIT 1", entity_type=entity_type, entity_id=entity_id, collaborator_user_id=collaborator_user_id)
            self._audit(conn, entity_type, entity_id, "add_collaboration", actor_user_id, {"collaborator_user_id": collaborator_user_id, "permission_level": permission_level})
            return self._row_to_collab(row[0])

    def remove_collaboration(self, entity_type: str, entity_id: str, collab_id: int, actor_user_id: str) -> bool:
        with self._conn() as conn:
            if entity_type == "script":
                entity = self.get_script(entity_id)
            elif entity_type == "scene":
                entity = self.get_scene(entity_id)
                if entity:
                    entity = self.get_script(entity.script_id)
            else:
                entity = self.get_storyboard(entity_id)
            if not entity:
                return False
            if entity.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can remove collaborations")
            conn.run("DELETE FROM storyboard_collaborations WHERE id = :id AND entity_type = :entity_type AND entity_id = :entity_id", id=collab_id, entity_type=entity_type, entity_id=entity_id)
            self._audit(conn, entity_type, entity_id, "remove_collaboration", actor_user_id, {"collab_id": collab_id})
            return True

    def list_collaborations(self, entity_type: str, entity_id: str) -> List[StoryboardCollaboration]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM storyboard_collaborations WHERE entity_type = :entity_type AND entity_id = :entity_id ORDER BY created_at DESC", entity_type=entity_type, entity_id=entity_id)
            return [self._row_to_collab(r) for r in rows]

    def _audit(self, conn, entity_type: str, entity_id: str, action: str, actor_user_id: str, details: Dict[str, Any]):
        conn.run(
            """
            INSERT INTO storyboard_audit (entity_type, entity_id, action, actor_user_id, details, created_at)
            VALUES (:entity_type, :entity_id, :action, :actor_user_id, :details, :created_at)
            """,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_user_id=actor_user_id,
            details=json.dumps(details),
            created_at=datetime.utcnow().isoformat(),
        )

    def migrate_existing_scripts(self, legacy_scripts: List[Dict[str, Any]]) -> Dict[str, int]:
        migrated = 0
        skipped = 0
        for legacy in legacy_scripts:
            if self.get_script(legacy["id"]):
                skipped += 1
                continue
            script = Script(
                id=legacy["id"],
                brand_id=legacy.get("brand_id", "vertep"),
                title=legacy.get("title", "Unnamed Script"),
                description=legacy.get("description", ""),
                status=legacy.get("status", "draft"),
                script_data=legacy.get("script_data", {}),
                owner_user_id=legacy.get("owner_user_id", "system"),
                active=legacy.get("active", True),
                created_at=legacy.get("created_at", datetime.utcnow().isoformat()),
                updated_at=legacy.get("updated_at", datetime.utcnow().isoformat()),
            )
            self.create_script(script)
            migrated += 1
        return {"migrated": migrated, "skipped": skipped}


def migrate_existing_scripts(db_url: str, legacy_scripts: List[Dict[str, Any]]) -> Dict[str, int]:
    module = StoryboardModule(db_url)
    return module.migrate_existing_scripts(legacy_scripts)