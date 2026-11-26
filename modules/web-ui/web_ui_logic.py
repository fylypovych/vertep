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
class UIConfig:
    id: str
    key: str
    value: Dict[str, Any]
    description: str
    updated_at: str


@dataclass
class UITheme:
    id: str
    name: str
    colors: Dict[str, str]
    is_default: bool
    created_at: str


@dataclass
class UINavigation:
    id: str
    label: str
    path: str
    icon: str
    order: int
    module_namespace: Optional[str]
    required_permissions: List[str]
    visible: bool


class WebUIModule:
    def __init__(self, db_url: str):
        self.db_url = db_url

    @contextmanager
    def _conn(self):
        conn = pg8000.native.Connection(self.db_url)
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_config(self, row) -> UIConfig:
        return UIConfig(
            id=row[0],
            key=row[1],
            value=json.loads(row[2]) if row[2] else {},
            description=row[3] or "",
            updated_at=str(row[4]),
        )

    def _row_to_theme(self, row) -> UITheme:
        return UITheme(
            id=row[0],
            name=row[1],
            colors=json.loads(row[2]) if row[2] else {},
            is_default=row[3],
            created_at=str(row[4]),
        )

    def _row_to_nav(self, row) -> UINavigation:
        return UINavigation(
            id=row[0],
            label=row[1],
            path=row[2],
            icon=row[3],
            order=row[4],
            module_namespace=row[5],
            required_permissions=json.loads(row[6]) if row[6] else [],
            visible=row[7],
        )

    # Config
    def get_config(self, key: str) -> Optional[UIConfig]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM ui_config WHERE key = :key", key=key)
            return self._row_to_config(rows[0]) if rows else None

    def set_config(self, key: str, value: Dict[str, Any], description: str = "") -> UIConfig:
        with self._conn() as conn:
            existing = self.get_config(key)
            now = datetime.utcnow().isoformat()
            if existing:
                conn.run(
                    "UPDATE ui_config SET value = :value, description = :description, updated_at = :updated_at WHERE key = :key",
                    value=json.dumps(value),
                    description=description,
                    updated_at=now,
                    key=key,
                )
            else:
                conn.run(
                    "INSERT INTO ui_config (key, value, description, updated_at) VALUES (:key, :value, :description, :updated_at)",
                    key=key,
                    value=json.dumps(value),
                    description=description,
                    updated_at=now,
                )
            return self.get_config(key)

    def list_configs(self) -> List[UIConfig]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM ui_config ORDER BY key")
            return [self._row_to_config(r) for r in rows]

    # Themes
    def get_theme(self, theme_id: str) -> Optional[UITheme]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM ui_themes WHERE id = :id", id=theme_id)
            return self._row_to_theme(rows[0]) if rows else None

    def get_default_theme(self) -> Optional[UITheme]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM ui_themes WHERE is_default = true LIMIT 1")
            return self._row_to_theme(rows[0]) if rows else None

    def list_themes(self) -> List[UITheme]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM ui_themes ORDER BY name")
            return [self._row_to_theme(r) for r in rows]

    def create_theme(self, theme: UITheme) -> UITheme:
        with self._conn() as conn:
            if theme.is_default:
                conn.run("UPDATE ui_themes SET is_default = false")
            conn.run(
                "INSERT INTO ui_themes (id, name, colors, is_default, created_at) VALUES (:id, :name, :colors, :is_default, :created_at)",
                id=theme.id,
                name=theme.name,
                colors=json.dumps(theme.colors),
                is_default=theme.is_default,
                created_at=theme.created_at,
            )
            return theme

    def update_theme(self, theme_id: str, updates: Dict[str, Any]) -> Optional[UITheme]:
        with self._conn() as conn:
            theme = self.get_theme(theme_id)
            if not theme:
                return None
            allowed = {"name", "colors", "is_default"}
            set_clauses = []
            params = {"id": theme_id}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k == "colors" else v
            if not set_clauses:
                return theme
            if "is_default" in updates and updates["is_default"]:
                conn.run("UPDATE ui_themes SET is_default = false")
            conn.run(f"UPDATE ui_themes SET {', '.join(set_clauses)} WHERE id = :id", **params)
            return self.get_theme(theme_id)

    def delete_theme(self, theme_id: str) -> bool:
        with self._conn() as conn:
            theme = self.get_theme(theme_id)
            if not theme:
                return False
            if theme.is_default:
                raise ValueError("Cannot delete default theme")
            conn.run("DELETE FROM ui_themes WHERE id = :id", id=theme_id)
            return True

    # Navigation
    def get_navigation(self, nav_id: str) -> Optional[UINavigation]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM ui_navigation WHERE id = :id", id=nav_id)
            return self._row_to_nav(rows[0]) if rows else None

    def list_navigation(self, visible_only: bool = True) -> List[UINavigation]:
        with self._conn() as conn:
            sql = "SELECT * FROM ui_navigation"
            if visible_only:
                sql += " WHERE visible = true"
            sql += " ORDER BY `order`"
            rows = conn.run(sql)
            return [self._row_to_nav(r) for r in rows]

    def create_navigation(self, nav: UINavigation) -> UINavigation:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO ui_navigation (id, label, path, icon, `order`, module_namespace, required_permissions, visible)
                VALUES (:id, :label, :path, :icon, :order, :module_namespace, :required_permissions, :visible)
                """,
                id=nav.id,
                label=nav.label,
                path=nav.path,
                icon=nav.icon,
                order=nav.order,
                module_namespace=nav.module_namespace,
                required_permissions=json.dumps(nav.required_permissions),
                visible=nav.visible,
            )
            return nav

    def update_navigation(self, nav_id: str, updates: Dict[str, Any]) -> Optional[UINavigation]:
        with self._conn() as conn:
            nav = self.get_navigation(nav_id)
            if not nav:
                return None
            allowed = {"label", "path", "icon", "order", "module_namespace", "required_permissions", "visible"}
            set_clauses = []
            params = {"id": nav_id}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = json.dumps(v) if k == "required_permissions" else v
            if not set_clauses:
                return nav
            conn.run(f"UPDATE ui_navigation SET {', '.join(set_clauses)} WHERE id = :id", **params)
            return self.get_navigation(nav_id)

    def delete_navigation(self, nav_id: str) -> bool:
        with self._conn() as conn:
            nav = self.get_navigation(nav_id)
            if not nav:
                return False
            conn.run("DELETE FROM ui_navigation WHERE id = :id", id=nav_id)
            return True

    # Module registry info
    def get_module_info(self) -> List[Dict[str, Any]]:
        return [
            {"namespace": "characters", "label": "Персонажі", "path": "/characters", "icon": "user", "enabled": True},
            {"namespace": "brands", "label": "Бренди", "path": "/brands", "icon": "tag", "enabled": True},
            {"namespace": "storyboard", "label": "Сторіборд", "path": "/storyboard", "icon": "film", "enabled": True},
            {"namespace": "publisher", "label": "Публікація", "path": "/published", "icon": "share", "enabled": True},
            {"namespace": "monitoring", "label": "Моніторинг", "path": "/monitoring", "icon": "activity", "enabled": True},
            {"namespace": "modules", "label": "Модулі", "path": "/modules", "icon": "package", "enabled": True},
            {"namespace": "settings", "label": "Налаштування", "path": "/settings", "icon": "settings", "enabled": True},
        ]


def migrate_existing_web_ui(db_url: str, legacy_data: Dict[str, Any]) -> Dict[str, int]:
    return {"migrated": 0, "skipped": 0}