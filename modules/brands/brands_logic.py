"""Brands Application Module logic (#500 AC1/AC2, #502).

The module is the single owner of brands, channels (M:N with brands) and
publication policies. It runs in its own container with its own schema/role;
the platform provisions the database and hands the credentials to the module.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
CHANNEL_TYPES = {"youtube", "tiktok", "facebook", "instagram", "threads", "telegram"}
BRAND_POLICIES = {"automatic", "approval_required"}
CHANNEL_POLICIES = {"inherit", "automatic", "approval_required"}


class BrandsError(ValueError):
    """Invalid request (422)."""


class NotFound(LookupError):
    """Unknown brand/channel (404)."""


class Conflict(RuntimeError):
    """State conflict (409)."""


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


class BrandsStore:
    """Data access over a DB-API connection whose search_path is the module schema."""

    def __init__(self, connect):
        self._connect = connect

    def _run(self, sql: str, args: tuple = ()) -> list:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(sql, args)
        return [list(row) for row in cursor.fetchall()] if cursor.description else []

    def _audit(self, brand_id: str | None, action: str, actor: str | None, details: dict) -> None:
        self._run("INSERT INTO brand_audit (brand_id, action, actor, details) VALUES (%s, %s, %s, %s)",
                  (brand_id, action, actor, _json(details)))

    # -- brands ----------------------------------------------------------------------
    @staticmethod
    def _brand(row) -> dict:
        return {"id": row[0], "name": row[1], "enabled": row[2], "metadata": _obj(row[3]),
                "publishing": _obj(row[4]), "publication_policy": row[5]}

    def list_brands(self) -> list[dict]:
        return [self._brand(row) for row in self._run(
            "SELECT id, name, enabled, metadata, publishing, publication_policy FROM brands ORDER BY id")]

    def get_brand(self, brand_id: str) -> dict | None:
        rows = self._run("SELECT id, name, enabled, metadata, publishing, publication_policy FROM brands WHERE id = %s",
                         (brand_id,))
        return self._brand(rows[0]) if rows else None

    @staticmethod
    def _validate_brand(body: dict) -> dict:
        brand_id = str(body.get("id", ""))
        if not SAFE_ID.fullmatch(brand_id):
            raise BrandsError("Invalid brand ID")
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 120:
            raise BrandsError("Brand name is required (up to 120 characters)")
        policy = body.get("publication_policy", "approval_required")
        if policy not in BRAND_POLICIES:
            raise BrandsError("publication_policy must be automatic or approval_required")
        for key in ("metadata", "publishing"):
            if not isinstance(body.get(key, {}), dict):
                raise BrandsError(f"{key} must be an object")
        return {"id": brand_id, "name": name, "enabled": bool(body.get("enabled", True)),
                "metadata": body.get("metadata") or {}, "publishing": body.get("publishing") or {},
                "publication_policy": policy}

    def create_brand(self, body: dict, actor: str | None) -> dict:
        brand = self._validate_brand(body)
        if self.get_brand(brand["id"]):
            raise Conflict("Brand already exists")
        self._run("INSERT INTO brands (id, name, enabled, metadata, publishing, publication_policy) "
                  "VALUES (%s, %s, %s, %s, %s, %s)",
                  (brand["id"], brand["name"], brand["enabled"], _json(brand["metadata"]),
                   _json(brand["publishing"]), brand["publication_policy"]))
        self._audit(brand["id"], "create", actor, {"name": brand["name"]})
        return self.get_brand(brand["id"])

    def update_brand(self, brand_id: str, body: dict, actor: str | None) -> dict:
        current = self.get_brand(brand_id)
        if current is None:
            raise NotFound("Brand not found")
        if body.get("id", brand_id) != brand_id:
            raise BrandsError("Brand ID cannot be changed")
        brand = self._validate_brand({**current, **body, "id": brand_id})
        self._run("UPDATE brands SET name = %s, enabled = %s, metadata = %s, publishing = %s, "
                  "publication_policy = %s, updated_at = now() WHERE id = %s",
                  (brand["name"], brand["enabled"], _json(brand["metadata"]), _json(brand["publishing"]),
                   brand["publication_policy"], brand_id))
        self._audit(brand_id, "update", actor, {key: body[key] for key in body if key != "id"})
        return self.get_brand(brand_id)

    def delete_brand(self, brand_id: str, actor: str | None) -> None:
        if self.get_brand(brand_id) is None:
            raise NotFound("Brand not found")
        self._run("DELETE FROM brands WHERE id = %s", (brand_id,))
        # A channel no brand uses any more stays as a record for history; it is
        # simply not offered to any brand.
        self._audit(brand_id, "delete", actor, {})

    # -- channels (M:N) ---------------------------------------------------------------
    @staticmethod
    def _channel(row, brand_id: str) -> dict:
        return {"channel_id": row[0], "brand_id": brand_id, "channel_type": row[1], "target": row[2],
                "enabled": row[3], "metadata": _obj(row[4]), "created_at": row[5].isoformat() if row[5] else None,
                "policy": row[6]}

    def list_channels(self, brand_id: str | None = None) -> list[dict]:
        sql = ("SELECT c.id, c.channel_type, c.target, c.enabled, c.metadata, c.created_at, l.policy, l.brand_id "
               "FROM channels c JOIN brand_channels l ON l.channel_id = c.id")
        rows = self._run(sql + (" WHERE l.brand_id = %s" if brand_id else "") + " ORDER BY l.brand_id, c.id",
                         (brand_id,) if brand_id else ())
        return [self._channel(row, row[7]) for row in rows]

    def get_channel(self, channel_id: str) -> dict | None:
        rows = self._run("SELECT c.id, c.channel_type, c.target, c.enabled, c.metadata, c.created_at, l.policy, l.brand_id "
                         "FROM channels c JOIN brand_channels l ON l.channel_id = c.id WHERE c.id = %s "
                         "ORDER BY l.brand_id LIMIT 1", (channel_id,))
        if not rows:
            return None
        channel = self._channel(rows[0], rows[0][7])
        channel["brands"] = [row[0] for row in self._run(
            "SELECT brand_id FROM brand_channels WHERE channel_id = %s ORDER BY brand_id", (channel_id,))]
        return channel

    def create_channel(self, brand_id: str, body: dict, actor: str | None, channel_id: str | None = None) -> dict:
        if self.get_brand(brand_id) is None:
            raise NotFound("Brand not found")
        channel_type = str(body.get("channel_type", ""))
        if channel_type not in CHANNEL_TYPES:
            raise BrandsError(f"Invalid channel type. Allowed: {', '.join(sorted(CHANNEL_TYPES))}")
        target = str(body.get("target", "")).strip()
        if not target or len(target) > 256:
            raise BrandsError("Channel target is required")
        policy = body.get("policy", "inherit")
        if policy not in CHANNEL_POLICIES:
            raise BrandsError("policy must be inherit, automatic or approval_required")
        if not isinstance(body.get("metadata", {}), dict):
            raise BrandsError("metadata must be an object")
        channel_id = channel_id or f"ch-{uuid.uuid4().hex[:12]}"
        self._run("INSERT INTO channels (id, channel_type, target, enabled, metadata) VALUES (%s, %s, %s, %s, %s)",
                  (channel_id, channel_type, target, bool(body.get("enabled", True)), _json(body.get("metadata"))))
        self._run("INSERT INTO brand_channels (brand_id, channel_id, policy) VALUES (%s, %s, %s)",
                  (brand_id, channel_id, policy))
        self._audit(brand_id, "channel_create", actor, {"channel_id": channel_id, "channel_type": channel_type})
        return self.get_channel(channel_id) | {"brand_id": brand_id, "policy": policy}

    def link_channel(self, brand_id: str, channel_id: str, policy: str, actor: str | None) -> dict:
        """Share an existing physical channel with another brand (no copy of it or its token)."""
        if self.get_brand(brand_id) is None:
            raise NotFound("Brand not found")
        if self.get_channel(channel_id) is None:
            raise NotFound("Channel not found")
        if policy not in CHANNEL_POLICIES:
            raise BrandsError("policy must be inherit, automatic or approval_required")
        self._run("INSERT INTO brand_channels (brand_id, channel_id, policy) VALUES (%s, %s, %s) "
                  "ON CONFLICT (brand_id, channel_id) DO UPDATE SET policy = EXCLUDED.policy",
                  (brand_id, channel_id, policy))
        self._audit(brand_id, "channel_link", actor, {"channel_id": channel_id, "policy": policy})
        return self.get_channel(channel_id)

    def update_channel(self, channel_id: str, body: dict, actor: str | None) -> dict:
        current = self.get_channel(channel_id)
        if current is None:
            raise NotFound("Channel not found")
        target = str(body.get("target", current["target"])).strip()
        if not target or len(target) > 256:
            raise BrandsError("Channel target is required")
        metadata = body.get("metadata", current["metadata"])
        if not isinstance(metadata, dict):
            raise BrandsError("metadata must be an object")
        self._run("UPDATE channels SET target = %s, enabled = %s, metadata = %s WHERE id = %s",
                  (target, bool(body.get("enabled", current["enabled"])), _json(metadata), channel_id))
        self._audit(current["brand_id"], "channel_update", actor, {"channel_id": channel_id})
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id: str, actor: str | None) -> None:
        current = self.get_channel(channel_id)
        if current is None:
            raise NotFound("Channel not found")
        self._run("DELETE FROM channels WHERE id = %s", (channel_id,))
        self._audit(current["brand_id"], "channel_delete", actor, {"channel_id": channel_id})

    def effective_policy(self, brand_id: str, channel_id: str) -> str:
        """Channel override, else the brand's policy (#502 AC3)."""
        rows = self._run("SELECT l.policy, b.publication_policy FROM brand_channels l JOIN brands b ON b.id = l.brand_id "
                         "WHERE l.brand_id = %s AND l.channel_id = %s", (brand_id, channel_id))
        if not rows:
            raise NotFound("Channel is not linked to this brand")
        return rows[0][1] if rows[0][0] == "inherit" else rows[0][0]

    # -- migration from CORE ------------------------------------------------------------
    def import_legacy(self, brands: list[dict], channels: list[dict], actor: str | None) -> dict:
        """Idempotent import of CORE's legacy brands/channels, keeping their IDs."""
        result = {"brands": [], "channels": [], "skipped": []}
        for item in brands:
            try:
                if self.get_brand(str(item.get("id", ""))):
                    result["skipped"].append({"brand": item.get("id"), "reason": "exists"})
                    continue
                self.create_brand(item, actor)
                result["brands"].append(item["id"])
            except (BrandsError, Conflict) as error:
                result["skipped"].append({"brand": item.get("id"), "reason": str(error)})
        for item in channels:
            channel_id = str(item.get("channel_id", ""))
            try:
                if self.get_channel(channel_id):
                    result["skipped"].append({"channel": channel_id, "reason": "exists"})
                    continue
                self.create_channel(str(item.get("brand_id", "")), item, actor, channel_id=channel_id or None)
                result["channels"].append(channel_id)
            except (BrandsError, NotFound) as error:
                result["skipped"].append({"channel": channel_id, "reason": str(error)})
        self._audit(None, "migrate", actor, {"brands": len(result["brands"]), "channels": len(result["channels"])})
        return result

    def audit_records(self, brand_id: str | None = None, limit: int = 200) -> list[dict]:
        rows = self._run("SELECT brand_id, action, actor, details, at FROM brand_audit"
                         + (" WHERE brand_id = %s" if brand_id else "") + " ORDER BY id DESC LIMIT %s",
                         ((brand_id, limit) if brand_id else (limit,)))
        return [{"brand_id": r[0], "action": r[1], "actor": r[2], "details": _obj(r[3]),
                 "at": r[4].isoformat() if r[4] else None} for r in rows]


def health_check() -> dict[str, Any]:
    return {"ready": True}
