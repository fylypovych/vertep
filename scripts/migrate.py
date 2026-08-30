"""Apply packaged PostgreSQL migrations once, before Core starts."""

import importlib.util
import json
import os
from pathlib import Path

import psycopg


def _load_backfill(path: Path):
    module_name = "vertep_backfill_" + "".join(
        character if character.isalnum() else "_" for character in path.name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load backfill {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run_batch", None)):
        raise RuntimeError(f"Backfill {path.name} must define run_batch(connection, checkpoint)")
    return module


def run_backfills(root: Path, connection) -> list[str]:
    """Run resumable, bounded batches and persist a checkpoint after every commit."""
    completed_now = []
    connection.execute("""CREATE TABLE IF NOT EXISTS migration_backfills(
        name TEXT PRIMARY KEY, checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
        batches BIGINT NOT NULL DEFAULT 0, completed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    connection.commit()
    maximum = int(os.getenv("BACKFILL_MAX_BATCHES_PER_RUN", "10000"))
    for path in sorted(root.glob("[0-9][0-9][0-9]_*.backfill.py")):
        module = _load_backfill(path)
        row = connection.execute(
            "SELECT checkpoint,completed_at FROM migration_backfills WHERE name=%s", (path.name,)
        ).fetchone()
        if row and row[1] is not None:
            continue
        checkpoint = dict(row[0]) if row else {}
        for _ in range(maximum):
            with connection.transaction():
                result = module.run_batch(connection, checkpoint)
                if not isinstance(result, dict) or not isinstance(result.get("done"), bool):
                    raise RuntimeError(f"Backfill {path.name} returned an invalid result")
                checkpoint = result.get("checkpoint") or checkpoint
                connection.execute("""INSERT INTO migration_backfills(name,checkpoint,batches,completed_at,updated_at)
                    VALUES(%s,%s::jsonb,1,CASE WHEN %s THEN now() END,now())
                    ON CONFLICT(name) DO UPDATE SET checkpoint=excluded.checkpoint,
                    batches=migration_backfills.batches+1,
                    completed_at=CASE WHEN %s THEN now() ELSE NULL END,updated_at=now()""",
                    (path.name, json.dumps(checkpoint), result["done"], result["done"]))
            if result["done"]:
                completed_now.append(path.name)
                break
        else:
            raise RuntimeError(f"Backfill {path.name} exceeded BACKFILL_MAX_BATCHES_PER_RUN")
    return completed_now


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
        applied_now.extend(run_backfills(root, connection))
    return applied_now


if __name__ == "__main__":
    for name in migrate(Path(os.getenv("MIGRATIONS_ROOT", "/app/db"))):
        print(f"Applied {name}")
