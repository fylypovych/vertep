-- AES-256-GCM envelope of the secret store; the key stays outside the database.
CREATE TABLE IF NOT EXISTS secret_store (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    envelope JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
