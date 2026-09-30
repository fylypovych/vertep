ALTER TABLE registered_nodes
    ADD COLUMN IF NOT EXISTS runtime_status TEXT NOT NULL DEFAULT 'PENDING_SELF_TEST';
ALTER TABLE registered_nodes
    ADD COLUMN IF NOT EXISTS self_test_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE registered_nodes
    ADD COLUMN IF NOT EXISTS last_self_test_at TIMESTAMPTZ;
ALTER TABLE registered_nodes
    ADD COLUMN IF NOT EXISTS enrollment_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS registered_nodes_enrollment_hash_idx
    ON registered_nodes(enrollment_hash) WHERE enrollment_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS registered_nodes_runtime_status_idx ON registered_nodes(runtime_status);
