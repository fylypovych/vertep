CREATE SCHEMA IF NOT EXISTS publishing;

CREATE TABLE IF NOT EXISTS publishing.publishing_channels (
    id VARCHAR(64) PRIMARY KEY,
    brand_id VARCHAR(64) NOT NULL,
    platform VARCHAR(32) NOT NULL CHECK (platform IN ('youtube', 'tiktok', 'facebook', 'instagram', 'threads', 'telegram')),
    channel_name VARCHAR(255) NOT NULL,
    channel_id VARCHAR(255) NOT NULL,
    credentials JSONB DEFAULT '{}',
    active BOOLEAN DEFAULT true,
    owner_user_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pub_channels_brand ON publishing.publishing_channels(brand_id);
CREATE INDEX IF NOT EXISTS idx_pub_channels_platform ON publishing.publishing_channels(platform);
CREATE INDEX IF NOT EXISTS idx_pub_channels_owner ON publishing.publishing_channels(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_pub_channels_active ON publishing.publishing_channels(active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_channels_brand_platform ON publishing.publishing_channels(brand_id, platform, channel_id);

CREATE TABLE IF NOT EXISTS publishing.publications (
    id VARCHAR(64) PRIMARY KEY,
    brand_id VARCHAR(64) NOT NULL,
    storyboard_id VARCHAR(64) NOT NULL,
    video_path VARCHAR(512) NOT NULL,
    title VARCHAR(512) NOT NULL,
    description TEXT DEFAULT '',
    tags JSONB DEFAULT '[]',
    scheduled_at TIMESTAMPTZ,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    platform_results JSONB DEFAULT '{}',
    owner_user_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_publications_brand ON publishing.publications(brand_id);
CREATE INDEX IF NOT EXISTS idx_publications_storyboard ON publishing.publications(storyboard_id);
CREATE INDEX IF NOT EXISTS idx_publications_owner ON publishing.publications(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_publications_status ON publishing.publications(status);
CREATE INDEX IF NOT EXISTS idx_publications_scheduled ON publishing.publications(scheduled_at);

CREATE TABLE IF NOT EXISTS publishing.publishing_collaborations (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL CHECK (entity_type IN ('channel', 'publication')),
    entity_id VARCHAR(64) NOT NULL,
    collaborator_user_id VARCHAR(64) NOT NULL,
    permission_level VARCHAR(16) NOT NULL CHECK (permission_level IN ('read', 'write', 'admin')),
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_type, entity_id, collaborator_user_id)
);

CREATE INDEX IF NOT EXISTS idx_pub_collabs_entity ON publishing.publishing_collaborations(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_pub_collabs_user ON publishing.publishing_collaborations(collaborator_user_id);

CREATE TABLE IF NOT EXISTS publishing.publishing_audit (
    id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor_user_id VARCHAR(64) NOT NULL,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pub_audit_entity ON publishing.publishing_audit(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_pub_audit_actor ON publishing.publishing_audit(actor_user_id);