The initial PostgreSQL schema is in `001_initial.sql`. The current file-backed
store remains the offline fallback; production startup should run this
migration before enabling a PostgreSQL repository implementation.
