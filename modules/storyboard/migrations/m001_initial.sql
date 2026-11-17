CREATE SCHEMA IF NOT EXISTS storyboard;

CREATE TABLE IF NOT EXISTS storyboard.scripts (
    id VARCHAR(64) PRIMARY KEY,
    brand_id VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    script_data JSONB DEFAULT '{}',
    owner_user_id VARCHAR(64) NOT NULL,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scripts_brand ON storyboard.scripts(brand_id);
CREATE INDEX IF NOT EXISTS idx_scripts_owner ON storyboard.scripts(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_scripts_active ON storyboard.scripts(active);
CREATE INDEX IF NOT EXISTS idx_scripts_status ON storyboard.scripts(status);

CREATE TABLE IF NOT EXISTS storyboard.scenes (
    id VARCHAR(64) PRIMARY KEY,
    script_id VARCHAR(64) NOT NULL REFERENCES storyboard.scripts(id) ON DELETE CASCADE,
    scene_number INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT DEFAULT '',
    scene_data JSONB DEFAULT '{}',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    generated_assets JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scenes_script ON storyboard.scenes(script_id);
CREATE INDEX IF NOT EXISTS idx_scenes_number ON storyboard.scenes(script_id, scene_number);
CREATE INDEX IF NOT EXISTS idx_scenes_status ON storyboard.scenes(status);

CREATE TABLE IF NOT EXISTS storyboard.storyboards (
    id VARCHAR(64) PRIMARY KEY,
    script_id VARCHAR(64) NOT NULL REFERENCES storyboard.scripts(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    panels JSONB DEFAULT '[]',
    owner_user_id VARCHAR(64) NOT NULL,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_storyboards_script ON storyboard.storyboards(script_id);
CREATE INDEX IF NOT EXISTS idx_storyboards_owner ON storyboard.storyboards(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_storyboards_active ON storyboard.storyboards(active);

CREATE TABLE IF NOT EXISTS storyboard.storyboard_collaborations (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL CHECK (entity_type IN ('script', 'scene', 'storyboard')),
    entity_id VARCHAR(64) NOT NULL,
    collaborator_user_id VARCHAR(64) NOT NULL,
    permission_level VARCHAR(16) NOT NULL CHECK (permission_level IN ('read', 'write', 'admin')),
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_type, entity_id, collaborator_user_id)
);

CREATE INDEX IF NOT EXISTS idx_sb_collabs_entity ON storyboard.storyboard_collaborations(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_sb_collabs_user ON storyboard.storyboard_collaborations(collaborator_user_id);

CREATE TABLE IF NOT EXISTS storyboard.storyboard_audit (
    id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor_user_id VARCHAR(64) NOT NULL,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sb_audit_entity ON storyboard.storyboard_audit(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_sb_audit_actor ON storyboard.storyboard_audit(actor_user_id);