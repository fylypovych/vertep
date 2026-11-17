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
class Brand:
    id: str
    name: str
    description: str
    logo_url: Optional[str]
    website: Optional[str]
    social_links: Dict[str, str]
    owner_user_id: str
    active: bool
    created_at: str
    updated_at: str


@dataclass
class BrandCollaboration:
    id: int
    brand_id: str
    collaborator_user_id: str
    permission_level: str  # read, write, admin
    active: bool
    created_at: str
    updated_at: str


class BrandsModule:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self._pool = None

    @contextmanager
    def _conn(self):
        conn = pg8000.native.Connection(self.db_url)
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_brand(self, row) -> Brand:
        return Brand(
            id=row[0],
            name=row[1],
            description=row[2] or "",
            logo_url=row[3],
            website=row[4],
            social_links=json.loads(row[5]) if row[5] else {},
            owner_user_id=row[6],
            active=row[7],
            created_at=str(row[8]),
            updated_at=str(row[9]),
        )

    def _row_to_collab(self, row) -> BrandCollaboration:
        return BrandCollaboration(
            id=row[0],
            brand_id=row[1],
            collaborator_user_id=row[2],
            permission_level=row[3],
            active=row[4],
            created_at=str(row[5]),
            updated_at=str(row[6]),
        )

    def create_brand(self, brand: Brand) -> Brand:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO brands (id, name, description, logo_url, website, social_links, owner_user_id, active, created_at, updated_at)
                VALUES (:id, :name, :description, :logo_url, :website, :social_links, :owner_user_id, :active, :created_at, :updated_at)
                """,
                id=brand.id,
                name=brand.name,
                description=brand.description,
                logo_url=brand.logo_url,
                website=brand.website,
                social_links=json.dumps(brand.social_links),
                owner_user_id=brand.owner_user_id,
                active=brand.active,
                created_at=brand.created_at,
                updated_at=brand.updated_at,
            )
            self._audit(conn, brand.id, "create", brand.owner_user_id, asdict(brand))
            return brand

    def get_brand(self, brand_id: str) -> Optional[Brand]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM brands WHERE id = :id", id=brand_id)
            return self._row_to_brand(rows[0]) if rows else None

    def list_brands(self, owner_user_id: Optional[str] = None, active_only: bool = False) -> List[Brand]:
        with self._conn() as conn:
            sql = "SELECT * FROM brands"
            params = {}
            conditions = []
            if owner_user_id:
                conditions.append("owner_user_id = :owner_user_id")
                params["owner_user_id"] = owner_user_id
            if active_only:
                conditions.append("active = true")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_brand(r) for r in rows]

    def update_brand(self, brand_id: str, updates: Dict[str, Any], actor_user_id: str) -> Optional[Brand]:
        with self._conn() as conn:
            brand = self.get_brand(brand_id)
            if not brand:
                return None
            if brand.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can update brand")

            allowed = {"name", "description", "logo_url", "website", "social_links", "active"}
            set_clauses = []
            params = {"id": brand_id, "updated_at": datetime.utcnow().isoformat()}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k == "social_links" else v
            if not set_clauses:
                return brand
            params["updated_at"] = datetime.utcnow().isoformat()
            set_clauses.append("updated_at = :updated_at")
            conn.run(f"UPDATE brands SET {', '.join(set_clauses)} WHERE id = :id", **params)
            self._audit(conn, brand_id, "update", actor_user_id, updates)
            return self.get_brand(brand_id)

    def delete_brand(self, brand_id: str, actor_user_id: str) -> bool:
        with self._conn() as conn:
            brand = self.get_brand(brand_id)
            if not brand:
                return False
            if brand.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can delete brand")
            conn.run("DELETE FROM brands WHERE id = :id", id=brand_id)
            self._audit(conn, brand_id, "delete", actor_user_id, {})
            return True

    def activate_brand(self, brand_id: str, actor_user_id: str) -> Optional[Brand]:
        return self.update_brand(brand_id, {"active": True}, actor_user_id)

    def deactivate_brand(self, brand_id: str, actor_user_id: str) -> Optional[Brand]:
        return self.update_brand(brand_id, {"active": False}, actor_user_id)

    def add_collaboration(self, brand_id: str, collaborator_user_id: str, permission_level: str, actor_user_id: str) -> BrandCollaboration:
        with self._conn() as conn:
            brand = self.get_brand(brand_id)
            if not brand:
                raise ValueError("Brand not found")
            if brand.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can add collaborations")
            if not brand.active:
                raise ValueError("Cannot add collaboration to inactive brand")
            if permission_level not in ("read", "write", "admin"):
                raise ValueError("Invalid permission level")
            now = datetime.utcnow().isoformat()
            conn.run(
                """
                INSERT INTO brand_collaborations (brand_id, collaborator_user_id, permission_level, active, created_at, updated_at)
                VALUES (:brand_id, :collaborator_user_id, :permission_level, true, :created_at, :updated_at)
                RETURNING id
                """,
                brand_id=brand_id,
                collaborator_user_id=collaborator_user_id,
                permission_level=permission_level,
                created_at=now,
                updated_at=now,
            )
            row = conn.run("SELECT * FROM brand_collaborations WHERE brand_id = :brand_id AND collaborator_user_id = :collaborator_user_id ORDER BY created_at DESC LIMIT 1", brand_id=brand_id, collaborator_user_id=collaborator_user_id)
            self._audit(conn, brand_id, "add_collaboration", actor_user_id, {"collaborator_user_id": collaborator_user_id, "permission_level": permission_level})
            return self._row_to_collab(row[0])

    def remove_collaboration(self, brand_id: str, collab_id: int, actor_user_id: str) -> bool:
        with self._conn() as conn:
            brand = self.get_brand(brand_id)
            if not brand:
                return False
            if brand.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can remove collaborations")
            conn.run("DELETE FROM brand_collaborations WHERE id = :id AND brand_id = :brand_id", id=collab_id, brand_id=brand_id)
            self._audit(conn, brand_id, "remove_collaboration", actor_user_id, {"collab_id": collab_id})
            return True

    def list_collaborations(self, brand_id: str) -> List[BrandCollaboration]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM brand_collaborations WHERE brand_id = :brand_id ORDER BY created_at DESC", brand_id=brand_id)
            return [self._row_to_collab(r) for r in rows]

    def _audit(self, conn, brand_id: str, action: str, actor_user_id: str, details: Dict[str, Any]):
        conn.run(
            """
            INSERT INTO brand_audit (brand_id, action, actor_user_id, details, created_at)
            VALUES (:brand_id, :action, :actor_user_id, :details, :created_at)
            """,
            brand_id=brand_id,
            action=action,
            actor_user_id=actor_user_id,
            details=json.dumps(details),
            created_at=datetime.utcnow().isoformat(),
        )

    def migrate_existing_brands(self, legacy_brands: List[Dict[str, Any]]) -> Dict[str, int]:
        migrated = 0
        skipped = 0
        for legacy in legacy_brands:
            if self.get_brand(legacy["id"]):
                skipped += 1
                continue
            brand = Brand(
                id=legacy["id"],
                name=legacy.get("name", "Unnamed Brand"),
                description=legacy.get("description", ""),
                logo_url=legacy.get("logo_url"),
                website=legacy.get("website"),
                social_links=legacy.get("social_links", {}),
                owner_user_id=legacy.get("owner_user_id", "system"),
                active=legacy.get("active", True),
                created_at=legacy.get("created_at", datetime.utcnow().isoformat()),
                updated_at=legacy.get("updated_at", datetime.utcnow().isoformat()),
            )
            self.create_brand(brand)
            migrated += 1
        return {"migrated": migrated, "skipped": skipped}


def migrate_existing_brands(db_url: str, legacy_brands: List[Dict[str, Any]]) -> Dict[str, int]:
    module = BrandsModule(db_url)
    return module.migrate_existing_brands(legacy_brands)