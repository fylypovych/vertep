"""Application Module data in the shared PostgreSQL: own schema, role, migrations.

Each module gets schema ``mod_<id>`` owned by login role ``mod_<id>``. The
role has no privileges on other schemas (other modules or the platform), so a
module reads and writes only its own data. Migrations run as that role with
``search_path`` pinned to the schema, one transaction per migration, recorded
in ``<schema>._vertep_migrations``; a failing migration rolls back entirely.

Export/restore use only the module's signed migrations (DDL) plus table data
(``COPY``), so an archive is restorable without ``pg_dump``.
"""

from __future__ import annotations

import csv
import io
import re
import secrets

import psycopg
from psycopg import sql

MIGRATION_TABLE = "_vertep_migrations"
OWNER_MARK = "vertep-module:"


class ModuleDataConflict(ValueError):
    """The schema or role a module ID maps to belongs to another module."""


def schema_name(module_id: str) -> str:
    return "mod_" + re.sub(r"[^a-z0-9_]", "_", module_id.lower())[:55]


def storage_conflict(module_id: str, other_id: str) -> bool:
    """Distinct module IDs whose schema or role would coincide (``a-b``/``a_b``,
    case, or a shared truncated prefix). The mapping itself stays unchanged so
    existing schemas keep their names; such a pair is rejected instead."""
    if module_id == other_id:
        return False
    first, second = schema_name(module_id), schema_name(other_id)
    return first == second or first[:50] == second[:50]


class ModuleDatabase:
    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn
        self._database: str | None = None

    def role_name(self, module_id: str) -> str:
        """Roles are cluster-wide; bind the name to this database so installations
        sharing a PostgreSQL cluster never share or drop each other's roles."""
        if self._database is None:
            with self._connect() as connection:
                self._database = connection.execute("SELECT current_database()").fetchone()[0]
        import hashlib
        return f"{schema_name(module_id)[:50]}_{hashlib.sha256(self._database.encode()).hexdigest()[:8]}"

    def _connect(self):
        # UTF8 even on SQL_ASCII databases: identifiers and text come back as str.
        return psycopg.connect(self.admin_dsn, autocommit=True, client_encoding="UTF8")

    # -- ownership -------------------------------------------------------------
    @staticmethod
    def _owners(connection, schema: str, role: str) -> list[str | None]:
        """Module IDs recorded on the existing schema and role (None: unmarked)."""
        owners = []
        row = connection.execute("SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname=%s",
                                 (schema,)).fetchone()
        if row is not None:
            owners.append(row[0])
        row = connection.execute("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s",
                                 (role,)).fetchone()
        if row is not None:
            owners.append(row[0])
        return owners

    def _check_owner(self, connection, module_id: str, schema: str, role: str) -> None:
        """Fail closed before any change when the schema/role is marked for another module."""
        mark = OWNER_MARK + module_id
        for owner in self._owners(connection, schema, role):
            if owner is not None and owner.startswith(OWNER_MARK) and owner != mark:
                raise ModuleDataConflict(f"Schema/role of module {module_id} belongs to {owner[len(OWNER_MARK):]}")

    # -- provisioning -----------------------------------------------------------
    def ensure(self, module_id: str, schema: str | None = None) -> dict:
        """Create role + schema if missing. Returns credentials for the platform secret store."""
        schema = schema or schema_name(module_id)
        role = self.role_name(module_id)
        password = secrets.token_urlsafe(24)
        with self._connect() as connection:
            self._check_owner(connection, module_id, schema, role)
            exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if exists:
                password = None  # existing credentials stay valid; restore never rotates them
            else:
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}")
                                   .format(sql.Identifier(role), sql.Literal(password)))
            # Owner of its own schema only; no CREATE elsewhere.
            connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(sql.Identifier(schema), sql.Identifier(role)))
            connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
            mark = sql.Literal(OWNER_MARK + module_id)
            connection.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(sql.Identifier(schema), mark))
            connection.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(role), mark))
            connection.execute(sql.SQL("ALTER ROLE {} SET search_path = {}").format(sql.Identifier(role), sql.Identifier(schema)))
            database = connection.execute("SELECT current_database()").fetchone()[0]
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
            with connection.transaction():
                self._as_module(connection, role, schema)
                connection.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} (id text PRIMARY KEY, version text NOT NULL, "
                                           "applied_at timestamptz NOT NULL DEFAULT now())")
                                   .format(sql.Identifier(schema), sql.Identifier(MIGRATION_TABLE)))
                connection.execute("RESET ROLE")
        return {"role": role, "schema": schema, "password": password, "database": database}

    @staticmethod
    def _as_module(connection, role: str, schema: str) -> None:
        connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(schema)))

    def exists(self, module_id: str, schema: str | None = None) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s",
                                      (schema or schema_name(module_id),)).fetchone() is not None

    def applied(self, module_id: str, schema: str | None = None) -> list[dict]:
        schema = schema or schema_name(module_id)
        with self._connect() as connection:
            rows = connection.execute(sql.SQL("SELECT id, version FROM {}.{} ORDER BY applied_at, id")
                                      .format(sql.Identifier(schema), sql.Identifier(MIGRATION_TABLE))).fetchall()
        return [{"id": row[0], "version": row[1]} for row in rows]

    def migrate(self, module_id: str, migrations: list[dict], sources: dict[str, str], schema: str | None = None) -> list[str]:
        """Apply missing migrations in declared order, each atomically, as the module role."""
        schema = schema or schema_name(module_id)
        role = self.role_name(module_id)
        done = {item["id"] for item in self.applied(module_id, schema)}
        applied = []
        with self._connect() as connection:
            for migration in migrations:
                if migration["id"] in done:
                    continue
                with connection.transaction():
                    self._as_module(connection, role, schema)
                    connection.execute(sources[migration["id"]])
                    connection.execute(sql.SQL("INSERT INTO {}.{} (id, version) VALUES (%s, %s)")
                                       .format(sql.Identifier(schema), sql.Identifier(MIGRATION_TABLE)),
                                       (migration["id"], migration["version"]))
                applied.append(migration["id"])
        return applied

    # -- export / restore ------------------------------------------------------------
    def _tables(self, connection, schema: str) -> list[str]:
        """Module tables in foreign-key dependency order (referenced tables first)."""
        tables = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s AND tablename<>%s ORDER BY tablename",
            (schema, MIGRATION_TABLE)).fetchall()]
        edges = {table: set() for table in tables}
        for child, parent in connection.execute(
                "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid=c.connamespace WHERE c.contype='f' AND n.nspname=%s", (schema,)).fetchall():
            child, parent = child.split(".")[-1].strip('"'), parent.split(".")[-1].strip('"')
            if child in edges and parent in edges and child != parent:
                edges[child].add(parent)
        ordered, seen = [], set()

        def visit(table):
            if table in seen:
                return
            seen.add(table)
            for parent in sorted(edges[table]):
                visit(parent)
            ordered.append(table)
        for table in tables:
            visit(table)
        return ordered

    def export(self, module_id: str, schema: str | None = None) -> dict:
        schema = schema or schema_name(module_id)
        with self._connect() as connection:
            data = {}
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                for table in self._tables(connection, schema):
                    buffer = io.StringIO()
                    with connection.cursor().copy(sql.SQL("COPY {}.{} TO STDOUT WITH (FORMAT csv, HEADER true)")
                                                  .format(sql.Identifier(schema), sql.Identifier(table))) as copy:
                        for chunk in copy:
                            buffer.write(bytes(chunk).decode("utf-8"))
                    data[table] = buffer.getvalue()
        return {"schema": schema, "migrations": self.applied(module_id, schema), "tables": data}

    def restore(self, module_id: str, exported: dict, sources: dict[str, str], schema: str | None = None) -> None:
        """Recreate the schema from migrations, then load table data, in one rebuild."""
        schema = schema or schema_name(module_id)
        self.drop_schema(schema, module_id)
        self.ensure(module_id, schema)
        self.migrate(module_id, exported["migrations"], sources, schema)
        with self._connect() as connection:
            with connection.transaction():
                # The export holds each table's full content: rows a migration seeded
                # (defaults such as a retention setting) are replaced, not duplicated.
                restored = [table for table in self._tables(connection, schema) if table in exported["tables"]]
                if restored:
                    connection.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(
                        sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table)) for table in restored)))
                for table in self._tables(connection, schema):
                    if table not in exported["tables"]:
                        continue
                    content = exported["tables"][table]
                    header = next(csv.reader(io.StringIO(content)), None)
                    if not header:
                        continue
                    columns = sql.SQL(", ").join(sql.Identifier(column) for column in header)
                    with connection.cursor().copy(sql.SQL("COPY {}.{} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)")
                                                  .format(sql.Identifier(schema), sql.Identifier(table), columns)) as copy:
                        copy.write(content.encode("utf-8"))
                for table in self._tables(connection, schema):
                    # Identity/serial sequences continue after restored rows.
                    for (sequence, column) in connection.execute(
                            "SELECT pg_get_serial_sequence(format('%%I.%%I', %s::text, %s::text), attname), attname FROM pg_attribute "
                            "WHERE attrelid = format('%%I.%%I', %s::text, %s::text)::regclass AND attnum > 0 AND NOT attisdropped",
                            (schema, table, schema, table)).fetchall():
                        if sequence:
                            connection.execute(sql.SQL("SELECT setval(%s, COALESCE((SELECT max({}) FROM {}.{}), 0) + 1, false)")
                                               .format(sql.Identifier(column), sql.Identifier(schema), sql.Identifier(table)),
                                               (sequence,))

    def fingerprint(self, module_id: str, schema: str | None = None) -> dict:
        exported = self.export(module_id, schema)
        import hashlib
        return {table: hashlib.sha256("\n".join(sorted(content.splitlines()[1:])).encode()).hexdigest()
                for table, content in exported["tables"].items()}

    # -- removal -----------------------------------------------------------------
    def drop_schema(self, schema: str, module_id: str | None = None) -> None:
        with self._connect() as connection:
            if module_id is not None:
                self._check_owner(connection, module_id, schema, self.role_name(module_id))
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    def drop(self, module_id: str) -> None:
        role = self.role_name(module_id)
        self.drop_schema(schema_name(module_id), module_id)
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                database = connection.execute("SELECT current_database()").fetchone()[0]
                connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(database), sql.Identifier(role)))
                connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
