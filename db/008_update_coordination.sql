CREATE TABLE IF NOT EXISTS update_fences (
    name TEXT PRIMARY KEY,
    epoch BIGINT NOT NULL,
    operation_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS migration_backfills (
    name TEXT PRIMARY KEY,
    checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
    batches BIGINT NOT NULL DEFAULT 0,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_operating_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    state TEXT NOT NULL,
    reason TEXT,
    operation_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rolling_update_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
