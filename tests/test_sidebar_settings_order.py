"""#518: shared sidebar items and settings tabs carry the same names in the same relative order."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sidebar_settings_items() -> list[tuple[str, str]]:
    text = (ROOT / "web-v2/src/app/layout/sidebar.component.ts").read_text(encoding="utf-8")
    body = text[text.index("navGroups: SidebarGroup[] = ["):text.index("moduleItems = signal")]
    items = []
    for block in re.finditer(r"id: 'settings-([a-z-]+)'.*?label: '([^']+)'", body, re.S):
        items.append((block.group(1), block.group(2)))
    return items


def settings_tabs() -> list[tuple[str, str]]:
    text = (ROOT / "web-v2/src/app/settings/settings.component.ts").read_text(encoding="utf-8")
    return re.findall(r"\{ id: '([a-z-]+)', label: '([^']+)'", text)


def test_shared_items_have_the_same_names_and_relative_order():
    menu, tabs = sidebar_settings_items(), settings_tabs()
    shared = [tab for tab, _ in menu if tab in dict(tabs)]
    assert len(shared) == len(menu) >= 10  # every settings item in the menu is a settings tab
    assert [tab for tab, _ in tabs if tab in shared] == shared
    labels = dict(tabs)
    assert all(labels[tab] == label for tab, label in menu)


def test_related_settings_stand_together():
    order = [tab for tab, _ in settings_tabs()]
    assert order.index("video-engine") - order.index("models") == 1
    assert order.index("workflow-mode") - order.index("video-engine") == 1
    assert order.index("users") - order.index("security") == 1
