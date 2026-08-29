CREATE TABLE IF NOT EXISTS node_revoked_certificates (
    serial TEXT PRIMARY KEY,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
