CREATE TABLE IF NOT EXISTS scenes (
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  scene_id TEXT NOT NULL,
  scene_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(job_id, scene_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  scene_id TEXT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size BIGINT NOT NULL CHECK(size >= 0),
  sha256 TEXT NOT NULL,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_attempts (
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  stage_name TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(job_id, stage_name, attempt)
);
CREATE INDEX IF NOT EXISTS idx_scenes_status ON scenes(job_id, status);
CREATE INDEX IF NOT EXISTS idx_artifacts_job_scene ON artifacts(job_id, scene_id);
