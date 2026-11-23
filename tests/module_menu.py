"""Menu entries of the active official modules whose pages are built into the panel.

The suite runs without their module containers; the panel shows and opens those pages only
from these entries (core/module/navigation.py), so browser tests serve them as READY modules.
"""
ACTIVE_MODULE_MENU = [
    {"module_id": "brands", "id": "brands", "label": "Бренди", "path": "/brands", "group": "content", "admin_only": False},
    {"module_id": "characters", "id": "characters", "label": "Персонажі", "path": "/characters", "group": "content",
     "admin_only": False},
    {"module_id": "publishing", "id": "publishing", "label": "Опубліковане", "path": "/published", "group": "content",
     "admin_only": False},
]


def with_official_pages(result: dict) -> dict:
    """A navigation answer extended with the official pages that are not already in it."""
    present = {item["path"] for item in result["items"]}
    return {**result, "items": result["items"] + [item for item in ACTIVE_MODULE_MENU if item["path"] not in present]}
