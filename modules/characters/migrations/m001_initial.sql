-- Characters module migration m001_initial (version 1.0.0)
-- Creates tables for character management with ownership and collaboration

-- Characters table with ownership
CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'uk',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    workflow TEXT,
    owner_brand_id TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Character profile files stored as JSONB
CREATE TABLE IF NOT EXISTS character_profiles (
    character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    system_prompt TEXT,
    voice JSONB,
    visual JSONB,
    generation JSONB,
    publishing JSONB,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- References (images)
CREATE TABLE IF NOT EXISTS character_references (
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (character_id, filename)
);

-- Collaborations: which brands can use which characters
CREATE TABLE IF NOT EXISTS character_collaborations (
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    brand_id TEXT NOT NULL,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    granted_by TEXT NOT NULL,
    PRIMARY KEY (character_id, brand_id)
);

-- Audit log
CREATE TABLE IF NOT EXISTS character_audit (
    id BIGSERIAL PRIMARY KEY,
    character_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_brand_id TEXT,
    details JSONB,
    at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_characters_owner ON characters(owner_brand_id);
CREATE INDEX IF NOT EXISTS idx_characters_active ON characters(active);
CREATE INDEX IF NOT EXISTS idx_collaborations_brand ON character_collaborations(brand_id);
CREATE INDEX IF NOT EXISTS idx_audit_character ON character_audit(character_id);
CREATE INDEX IF NOT EXISTS idx_audit_at ON character_audit(at);