-- Brands Application Module (#500 AC1/AC2, #502): the single owner of brands,
-- channels and publication policies. Tables live in the module's own schema
-- (search_path is pinned by the platform), never in a fixed schema name.

CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}',
    publishing JSONB NOT NULL DEFAULT '{}',
    -- automatic | approval_required (#502 AC3)
    publication_policy TEXT NOT NULL DEFAULT 'approval_required'
        CHECK (publication_policy IN ('automatic', 'approval_required')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One physical channel (connection/target); credentials stay in the platform
-- secret store and are never copied here.
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,
    target TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Channel <-> Brands is M:N: a shared channel is linked, not duplicated.
-- policy: inherit | automatic | approval_required (#502 AC3).
CREATE TABLE IF NOT EXISTS brand_channels (
    brand_id TEXT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    policy TEXT NOT NULL DEFAULT 'inherit' CHECK (policy IN ('inherit', 'automatic', 'approval_required')),
    PRIMARY KEY (brand_id, channel_id)
);

CREATE TABLE IF NOT EXISTS brand_audit (
    id BIGSERIAL PRIMARY KEY,
    brand_id TEXT,
    action TEXT NOT NULL,
    actor TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
