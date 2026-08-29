CREATE TABLE IF NOT EXISTS node_registration_tokens (
    token_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS registered_nodes (
    node_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    hardware JSONB NOT NULL DEFAULT '{}'::jsonb,
    version TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    credential_generation INTEGER NOT NULL DEFAULT 1,
    certificate_serial TEXT,
    certificate_expires_at TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS registered_nodes_status_idx ON registered_nodes(status);
CREATE INDEX IF NOT EXISTS node_registration_tokens_expiry_idx ON node_registration_tokens(expires_at);
