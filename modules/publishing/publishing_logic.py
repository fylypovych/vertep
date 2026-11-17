import os
import json
import logging
import pg8000.native
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
from contextlib import contextmanager
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


PLATFORMS = ["youtube", "tiktok", "facebook", "instagram", "threads", "telegram"]


@dataclass
class PublishingChannel:
    id: str
    brand_id: str
    platform: str
    channel_name: str
    channel_id: str
    credentials: Dict[str, Any]
    active: bool
    owner_user_id: str
    created_at: str
    updated_at: str


@dataclass
class Publication:
    id: str
    brand_id: str
    storyboard_id: str
    video_path: str
    title: str
    description: str
    tags: List[str]
    scheduled_at: Optional[str]
    status: str  # pending, scheduled, publishing, published, failed
    platform_results: Dict[str, Any]
    owner_user_id: str
    created_at: str
    updated_at: str


@dataclass
class PublishingCollaboration:
    id: int
    entity_type: str  # channel, publication
    entity_id: str
    collaborator_user_id: str
    permission_level: str  # read, write, admin
    active: bool
    created_at: str
    updated_at: str


class PublisherAdapter(ABC):
    @abstractmethod
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @abstractmethod
    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        pass


class YouTubeAdapter(PublisherAdapter):
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        # Implementation would use YouTube Data API v3
        logger.info(f"Publishing to YouTube: {title}")
        return {"platform": "youtube", "status": "published", "video_id": "mock_youtube_id", "url": "https://youtube.com/watch?v=mock"}

    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return "access_token" in credentials and "refresh_token" in credentials


class TikTokAdapter(PublisherAdapter):
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Publishing to TikTok: {title}")
        return {"platform": "tiktok", "status": "published", "video_id": "mock_tiktok_id", "url": "https://tiktok.com/@user/video/mock"}

    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return "access_token" in credentials


class FacebookAdapter(PublisherAdapter):
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Publishing to Facebook: {title}")
        return {"platform": "facebook", "status": "published", "post_id": "mock_fb_id", "url": "https://facebook.com/mock"}

    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return "access_token" in credentials and "page_id" in credentials


class InstagramAdapter(PublisherAdapter):
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Publishing to Instagram: {title}")
        return {"platform": "instagram", "status": "published", "media_id": "mock_ig_id", "url": "https://instagram.com/p/mock"}

    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return "access_token" in credentials and "business_account_id" in credentials


class ThreadsAdapter(PublisherAdapter):
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Publishing to Threads: {title}")
        return {"platform": "threads", "status": "published", "post_id": "mock_threads_id", "url": "https://threads.net/@user/post/mock"}

    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return "access_token" in credentials


class TelegramAdapter(PublisherAdapter):
    def publish(self, video_path: str, title: str, description: str, tags: List[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Publishing to Telegram: {title}")
        return {"platform": "telegram", "status": "published", "message_id": "mock_tg_id", "url": "https://t.me/channel/mock"}

    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return "bot_token" in credentials and "chat_id" in credentials


ADAPTERS = {
    "youtube": YouTubeAdapter(),
    "tiktok": TikTokAdapter(),
    "facebook": FacebookAdapter(),
    "instagram": InstagramAdapter(),
    "threads": ThreadsAdapter(),
    "telegram": TelegramAdapter(),
}


class PublishingModule:
    def __init__(self, db_url: str):
        self.db_url = db_url

    @contextmanager
    def _conn(self):
        conn = pg8000.native.Connection(self.db_url)
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_channel(self, row) -> PublishingChannel:
        return PublishingChannel(
            id=row[0],
            brand_id=row[1],
            platform=row[2],
            channel_name=row[3],
            channel_id=row[4],
            credentials=json.loads(row[5]) if row[5] else {},
            active=row[6],
            owner_user_id=row[7],
            created_at=str(row[8]),
            updated_at=str(row[9]),
        )

    def _row_to_publication(self, row) -> Publication:
        return Publication(
            id=row[0],
            brand_id=row[1],
            storyboard_id=row[2],
            video_path=row[3],
            title=row[4],
            description=row[5] or "",
            tags=json.loads(row[6]) if row[6] else [],
            scheduled_at=row[7],
            status=row[8],
            platform_results=json.loads(row[9]) if row[9] else {},
            owner_user_id=row[10],
            created_at=str(row[11]),
            updated_at=str(row[12]),
        )

    def _row_to_collab(self, row) -> PublishingCollaboration:
        return PublishingCollaboration(
            id=row[0],
            entity_type=row[1],
            entity_id=row[2],
            collaborator_user_id=row[3],
            permission_level=row[4],
            active=row[5],
            created_at=str(row[6]),
            updated_at=str(row[7]),
        )

    # Channel CRUD
    def create_channel(self, channel: PublishingChannel) -> PublishingChannel:
        with self._conn() as conn:
            if channel.platform not in PLATFORMS:
                raise ValueError(f"Invalid platform: {channel.platform}")
            if not ADAPTERS[channel.platform].validate_credentials(channel.credentials):
                raise ValueError(f"Invalid credentials for {channel.platform}")
            conn.run(
                """
                INSERT INTO publishing_channels (id, brand_id, platform, channel_name, channel_id, credentials, active, owner_user_id, created_at, updated_at)
                VALUES (:id, :brand_id, :platform, :channel_name, :channel_id, :credentials, :active, :owner_user_id, :created_at, :updated_at)
                """,
                id=channel.id,
                brand_id=channel.brand_id,
                platform=channel.platform,
                channel_name=channel.channel_name,
                channel_id=channel.channel_id,
                credentials=json.dumps(channel.credentials),
                active=channel.active,
                owner_user_id=channel.owner_user_id,
                created_at=channel.created_at,
                updated_at=channel.updated_at,
            )
            self._audit(conn, "channel", channel.id, "create", channel.owner_user_id, asdict(channel))
            return channel

    def get_channel(self, channel_id: str) -> Optional[PublishingChannel]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM publishing_channels WHERE id = :id", id=channel_id)
            return self._row_to_channel(rows[0]) if rows else None

    def list_channels(self, brand_id: Optional[str] = None, platform: Optional[str] = None, owner_user_id: Optional[str] = None, active_only: bool = False) -> List[PublishingChannel]:
        with self._conn() as conn:
            sql = "SELECT * FROM publishing_channels"
            params = {}
            conditions = []
            if brand_id:
                conditions.append("brand_id = :brand_id")
                params["brand_id"] = brand_id
            if platform:
                conditions.append("platform = :platform")
                params["platform"] = platform
            if owner_user_id:
                conditions.append("owner_user_id = :owner_user_id")
                params["owner_user_id"] = owner_user_id
            if active_only:
                conditions.append("active = true")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_channel(r) for r in rows]

    def update_channel(self, channel_id: str, updates: Dict[str, Any], actor_user_id: str) -> Optional[PublishingChannel]:
        with self._conn() as conn:
            channel = self.get_channel(channel_id)
            if not channel:
                return None
            if channel.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can update channel")

            allowed = {"channel_name", "channel_id", "credentials", "active"}
            set_clauses = []
            params = {"id": channel_id, "updated_at": datetime.utcnow().isoformat()}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k == "credentials" else v
            if not set_clauses:
                return channel
            set_clauses.append("updated_at = :updated_at")
            conn.run(f"UPDATE publishing_channels SET {', '.join(set_clauses)} WHERE id = :id", **params)
            self._audit(conn, "channel", channel_id, "update", actor_user_id, updates)
            return self.get_channel(channel_id)

    def delete_channel(self, channel_id: str, actor_user_id: str) -> bool:
        with self._conn() as conn:
            channel = self.get_channel(channel_id)
            if not channel:
                return False
            if channel.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can delete channel")
            conn.run("DELETE FROM publishing_channels WHERE id = :id", id=channel_id)
            self._audit(conn, "channel", channel_id, "delete", actor_user_id, {})
            return True

    # Publication CRUD
    def create_publication(self, publication: Publication) -> Publication:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO publications (id, brand_id, storyboard_id, video_path, title, description, tags, scheduled_at, status, platform_results, owner_user_id, created_at, updated_at)
                VALUES (:id, :brand_id, :storyboard_id, :video_path, :title, :description, :tags, :scheduled_at, :status, :platform_results, :owner_user_id, :created_at, :updated_at)
                """,
                id=publication.id,
                brand_id=publication.brand_id,
                storyboard_id=publication.storyboard_id,
                video_path=publication.video_path,
                title=publication.title,
                description=publication.description,
                tags=json.dumps(publication.tags),
                scheduled_at=publication.scheduled_at,
                status=publication.status,
                platform_results=json.dumps(publication.platform_results),
                owner_user_id=publication.owner_user_id,
                created_at=publication.created_at,
                updated_at=publication.updated_at,
            )
            self._audit(conn, "publication", publication.id, "create", publication.owner_user_id, asdict(publication))
            return publication

    def get_publication(self, publication_id: str) -> Optional[Publication]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM publications WHERE id = :id", id=publication_id)
            return self._row_to_publication(rows[0]) if rows else None

    def list_publications(self, brand_id: Optional[str] = None, storyboard_id: Optional[str] = None, owner_user_id: Optional[str] = None, status: Optional[str] = None) -> List[Publication]:
        with self._conn() as conn:
            sql = "SELECT * FROM publications"
            params = {}
            conditions = []
            if brand_id:
                conditions.append("brand_id = :brand_id")
                params["brand_id"] = brand_id
            if storyboard_id:
                conditions.append("storyboard_id = :storyboard_id")
                params["storyboard_id"] = storyboard_id
            if owner_user_id:
                conditions.append("owner_user_id = :owner_user_id")
                params["owner_user_id"] = owner_user_id
            if status:
                conditions.append("status = :status")
                params["status"] = status
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_publication(r) for r in rows]

    def update_publication(self, publication_id: str, updates: Dict[str, Any], actor_user_id: str) -> Optional[Publication]:
        with self._conn() as conn:
            pub = self.get_publication(publication_id)
            if not pub:
                return None
            if pub.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can update publication")

            allowed = {"title", "description", "tags", "scheduled_at", "status", "platform_results", "video_path"}
            set_clauses = []
            params = {"id": publication_id, "updated_at": datetime.utcnow().isoformat()}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k in ("tags", "platform_results") else v
            if not set_clauses:
                return pub
            set_clauses.append("updated_at = :updated_at")
            conn.run(f"UPDATE publications SET {', '.join(set_clauses)} WHERE id = :id", **params)
            self._audit(conn, "publication", publication_id, "update", actor_user_id, updates)
            return self.get_publication(publication_id)

    def delete_publication(self, publication_id: str, actor_user_id: str) -> bool:
        with self._conn() as conn:
            pub = self.get_publication(publication_id)
            if not pub:
                return False
            if pub.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can delete publication")
            conn.run("DELETE FROM publications WHERE id = :id", id=publication_id)
            self._audit(conn, "publication", publication_id, "delete", actor_user_id, {})
            return True

    # Publish to platforms
    def publish_to_platforms(self, publication_id: str, platform_channels: Dict[str, str], actor_user_id: str) -> Dict[str, Any]:
        """platform_channels: {platform: channel_id}"""
        with self._conn() as conn:
            pub = self.get_publication(publication_id)
            if not pub:
                raise ValueError("Publication not found")
            if pub.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can publish")

            results = {}
            for platform, channel_id in platform_channels.items():
                if platform not in PLATFORMS:
                    results[platform] = {"status": "failed", "error": "Invalid platform"}
                    continue
                channel = self.get_channel(channel_id)
                if not channel or channel.platform != platform:
                    results[platform] = {"status": "failed", "error": "Channel not found or platform mismatch"}
                    continue
                if not channel.active:
                    results[platform] = {"status": "failed", "error": "Channel not active"}
                    continue
                try:
                    adapter = ADAPTERS[platform]
                    result = adapter.publish(pub.video_path, pub.title, pub.description, pub.tags, channel.credentials)
                    results[platform] = result
                except Exception as e:
                    logger.exception(f"Publish to {platform} failed")
                    results[platform] = {"status": "failed", "error": str(e)}

            # Update publication with results
            all_published = all(r.get("status") == "published" for r in results.values())
            any_published = any(r.get("status") == "published" for r in results.values())
            new_status = "published" if all_published else ("partial" if any_published else "failed")
            conn.run(
                "UPDATE publications SET status = :status, platform_results = :results, updated_at = :updated_at WHERE id = :id",
                status=new_status,
                results=json.dumps(results),
                updated_at=datetime.utcnow().isoformat(),
                id=publication_id,
            )
            self._audit(conn, "publication", publication_id, "publish", actor_user_id, {"results": results})
            return results

    # Collaborations
    def add_collaboration(self, entity_type: str, entity_id: str, collaborator_user_id: str, permission_level: str, actor_user_id: str) -> PublishingCollaboration:
        with self._conn() as conn:
            if entity_type not in ("channel", "publication"):
                raise ValueError("Invalid entity type")
            if permission_level not in ("read", "write", "admin"):
                raise ValueError("Invalid permission level")
            # Check ownership
            if entity_type == "channel":
                entity = self.get_channel(entity_id)
            else:
                entity = self.get_publication(entity_id)
            if not entity:
                raise ValueError("Entity not found")
            if entity.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can add collaborations")
            now = datetime.utcnow().isoformat()
            conn.run(
                """
                INSERT INTO publishing_collaborations (entity_type, entity_id, collaborator_user_id, permission_level, active, created_at, updated_at)
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
            row = conn.run("SELECT * FROM publishing_collaborations WHERE entity_type = :entity_type AND entity_id = :entity_id AND collaborator_user_id = :collaborator_user_id ORDER BY created_at DESC LIMIT 1", entity_type=entity_type, entity_id=entity_id, collaborator_user_id=collaborator_user_id)
            self._audit(conn, entity_type, entity_id, "add_collaboration", actor_user_id, {"collaborator_user_id": collaborator_user_id, "permission_level": permission_level})
            return self._row_to_collab(row[0])

    def remove_collaboration(self, entity_type: str, entity_id: str, collab_id: int, actor_user_id: str) -> bool:
        with self._conn() as conn:
            if entity_type == "channel":
                entity = self.get_channel(entity_id)
            else:
                entity = self.get_publication(entity_id)
            if not entity:
                return False
            if entity.owner_user_id != actor_user_id:
                raise PermissionError("Only owner can remove collaborations")
            conn.run("DELETE FROM publishing_collaborations WHERE id = :id AND entity_type = :entity_type AND entity_id = :entity_id", id=collab_id, entity_type=entity_type, entity_id=entity_id)
            self._audit(conn, entity_type, entity_id, "remove_collaboration", actor_user_id, {"collab_id": collab_id})
            return True

    def list_collaborations(self, entity_type: str, entity_id: str) -> List[PublishingCollaboration]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM publishing_collaborations WHERE entity_type = :entity_type AND entity_id = :entity_id ORDER BY created_at DESC", entity_type=entity_type, entity_id=entity_id)
            return [self._row_to_collab(r) for r in rows]

    def _audit(self, conn, entity_type: str, entity_id: str, action: str, actor_user_id: str, details: Dict[str, Any]):
        conn.run(
            """
            INSERT INTO publishing_audit (entity_type, entity_id, action, actor_user_id, details, created_at)
            VALUES (:entity_type, :entity_id, :action, :actor_user_id, :details, :created_at)
            """,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_user_id=actor_user_id,
            details=json.dumps(details),
            created_at=datetime.utcnow().isoformat(),
        )

    def migrate_existing_channels(self, legacy_channels: List[Dict[str, Any]]) -> Dict[str, int]:
        migrated = 0
        skipped = 0
        for legacy in legacy_channels:
            if self.get_channel(legacy["id"]):
                skipped += 1
                continue
            channel = PublishingChannel(
                id=legacy["id"],
                brand_id=legacy.get("brand_id", "vertep"),
                platform=legacy.get("platform", "youtube"),
                channel_name=legacy.get("channel_name", "Unnamed Channel"),
                channel_id=legacy.get("channel_id", ""),
                credentials=legacy.get("credentials", {}),
                active=legacy.get("active", True),
                owner_user_id=legacy.get("owner_user_id", "system"),
                created_at=legacy.get("created_at", datetime.utcnow().isoformat()),
                updated_at=legacy.get("updated_at", datetime.utcnow().isoformat()),
            )
            self.create_channel(channel)
            migrated += 1
        return {"migrated": migrated, "skipped": skipped}


def migrate_existing_channels(db_url: str, legacy_channels: List[Dict[str, Any]]) -> Dict[str, int]:
    module = PublishingModule(db_url)
    return module.migrate_existing_channels(legacy_channels)