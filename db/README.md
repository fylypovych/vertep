The initial PostgreSQL schema is in `001_initial.sql`. The current file-backed
store remains the offline fallback; production startup should run this
migration before enabling a PostgreSQL repository implementation.

Resumable data backfills use files named `NNN_name.backfill.py`. Each module
must define `run_batch(connection, checkpoint)` and return
`{"done": bool, "checkpoint": {...}}`. Every batch is committed separately and
recorded in `migration_backfills`, so the migration container resumes safely
after a restart instead of repeating completed work.
