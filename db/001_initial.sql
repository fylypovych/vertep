CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  character_id TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 5,
  source TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS job_events (
  id BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
  node_name TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  last_seen TIMESTAMPTZ NOT NULL,
  capabilities JSONB NOT NULL DEFAULT '{}'::jsonb
);
