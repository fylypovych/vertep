The initial PostgreSQL schema is in `001_initial.sql`. The current file-backed
store remains the offline fallback; production startup should run this
migration before enabling a PostgreSQL repository implementation.

The release manifest's `database_schema` is the highest migration number in this
directory. The migration strategy cannot be inferred from SQL, so a release that
is not a plain additive one declares it in an optional `db/compatibility.json`:
`{"strategy": "expand|contract|none", "rollback_safe": true|false,
"minimum_version": "0.0.2.90"}`. Without the file the defaults are `expand`,
`true` and `0.0.0.1`.

Resumable data backfills use files named `NNN_name.backfill.py`. Each module
must define `run_batch(connection, checkpoint)` and return
`{"done": bool, "checkpoint": {...}}`. Every batch is committed separately and
recorded in `migration_backfills`, so the migration container resumes safely
after a restart instead of repeating completed work.
