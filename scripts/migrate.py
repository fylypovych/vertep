"""Apply packaged PostgreSQL migrations once, before Core starts."""

import os
from pathlib import Path

import psycopg


def migrate(root: Path) -> list[str]:
    applied_now = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for migration in sorted(root.glob("[0-9][0-9][0-9]_*.sql")):
            if migration.name in applied:
                continue
            with connection.transaction():
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (migration.name,))
            applied_now.append(migration.name)
    return applied_now


if __name__ == "__main__":
    for name in migrate(Path(os.getenv("MIGRATIONS_ROOT", "/app/db"))):
        print(f"Applied {name}")
