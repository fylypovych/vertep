from pathlib import Path


def application_version() -> str:
    path = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0.0"
    return value if value else "0.0.0.0"
