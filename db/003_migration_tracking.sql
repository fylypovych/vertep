CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON jobs(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_workers_last_seen ON workers(last_seen);
CREATE INDEX IF NOT EXISTS idx_task_attempts_job ON task_attempts(job_id, created_at);
