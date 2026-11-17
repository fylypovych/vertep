CREATE SCHEMA IF NOT EXISTS brands;

CREATE TABLE IF NOT EXISTS brands.brands (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT DEFAULT '',
    logo_url VARCHAR(512),
    website VARCHAR(512),
    social_links JSONB DEFAULT '{}',
    owner_user_id VARCHAR(64) NOT NULL,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_brands_owner ON brands.brands(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_brands_active ON brands.brands(active);

CREATE TABLE IF NOT EXISTS brands.brand_collaborations (
    id SERIAL PRIMARY KEY,
    brand_id VARCHAR(64) NOT NULL REFERENCES brands.brands(id) ON DELETE CASCADE,
    collaborator_user_id VARCHAR(64) NOT NULL,
    permission_level VARCHAR(16) NOT NULL CHECK (permission_level IN ('read', 'write', 'admin')),
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (brand_id, collaborator_user_id)
);

CREATE INDEX IF NOT EXISTS idx_brand_collabs_brand ON brands.brand_collaborations(brand_id);
CREATE INDEX IF NOT EXISTS idx_brand_collabs_user ON brands.brand_collaborations(collaborator_user_id);

CREATE TABLE IF NOT EXISTS brands.brand_audit (
    id BIGSERIAL PRIMARY KEY,
    brand_id VARCHAR(64) NOT NULL REFERENCES brands.brands(id) ON DELETE CASCADE,
    action VARCHAR(32) NOT NULL,
    actor_user_id VARCHAR(64) NOT NULL,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_brand_audit_brand ON brands.brand_audit(brand_id);
CREATE INDEX IF NOT EXISTS idx_brand_audit_actor ON brands.brand_audit(actor_user_id);