"""i.0.0.4.78 (#478): late-restored notes of 0.0.3.65, without rewriting history."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "releases" / "0.0.3.65.md"
FILES = ["bootstrap.sh", "install.sh", "scripts/vertep", "installer/detect-gpu.sh",
         "installer/install-comfyui.sh", "installer/preflight.sh", "installer/roles/backup.sh",
         "installer/roles/monitoring.sh", "installer/roles/publisher.sh", "installer/roles/text.sh",
         "installer/roles/voice.sh"]


def test_header_is_exactly_the_version():
    assert NOTES.read_text(encoding="utf-8").splitlines()[0] == "# Vertep 0.0.3.65"


def test_content_matches_the_commit_and_changelog():
    text = NOTES.read_text(encoding="utf-8")
    assert "100755 → 100644" in text and "вміст файлів не змінювався" in text
    assert all(f"`{name}`" in text for name in FILES)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## ПРАВИЛЬНА НАЗВА: 0.0.3.65\n- Помилково знято ознаку виконуваного файла" in changelog


def test_late_restoration_is_stated_and_history_is_untouched():
    text = NOTES.read_text(encoding="utf-8")
    assert "відновлено пізніше" in text and "aeb4533a" in text
    assert "історію, SHA і tags не змінено" in text
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() != "0.0.3.65"
