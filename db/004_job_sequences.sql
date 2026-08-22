CREATE TABLE IF NOT EXISTS job_sequences (
  year INTEGER PRIMARY KEY,
  next_value BIGINT NOT NULL CHECK(next_value >= 0)
);
INSERT INTO job_sequences(year,next_value)
SELECT EXTRACT(YEAR FROM now())::INTEGER,
       COALESCE(MAX(split_part(job_id, '-', 2)::BIGINT), 0)
FROM jobs
WHERE split_part(job_id, '-', 1) = EXTRACT(YEAR FROM now())::TEXT
ON CONFLICT(year) DO NOTHING;
