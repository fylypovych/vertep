-- Characters module migration m002_knowledge_memory (version 0.0.4.18, #506)
-- Adds Character Knowledge Library, Worker extraction chunks, Risk-based Approval,
-- Knowledge Revocation & Incidents, Context API snapshots, Timelines, Versioned Canon,
-- Episodic Memory, and Beliefs inside the Characters module schema.

CREATE TABLE IF NOT EXISTS character_knowledge_sources (
    source_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'text',
    language TEXT NOT NULL DEFAULT 'uk',
    version INTEGER NOT NULL DEFAULT 1,
    sha256 TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    license_info TEXT NOT NULL DEFAULT '',
    rights_status TEXT NOT NULL DEFAULT 'unverified',
    quarantine_reason TEXT,
    file_path TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS character_knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES character_knowledge_sources(source_id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    source_version INTEGER NOT NULL DEFAULT 1,
    page_number INTEGER,
    section_title TEXT,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    ocr_used BOOLEAN NOT NULL DEFAULT FALSE,
    ocr_confidence REAL,
    injection_flagged BOOLEAN NOT NULL DEFAULT FALSE,
    injection_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS character_timelines (
    timeline_id TEXT NOT NULL,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'main',
    parent_timeline_id TEXT,
    branch_point_order INTEGER,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (character_id, timeline_id)
);

CREATE TABLE IF NOT EXISTS character_knowledge_items (
    item_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    source_id TEXT REFERENCES character_knowledge_sources(source_id) ON DELETE SET NULL,
    source_version INTEGER,
    page_number INTEGER,
    layer TEXT NOT NULL DEFAULT 'knowledge',
    item_type TEXT NOT NULL DEFAULT 'fact',
    truth_status TEXT NOT NULL DEFAULT 'verified',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence REAL NOT NULL DEFAULT 0.8,
    risk_level TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    timeline_id TEXT NOT NULL DEFAULT 'main',
    timeline_order INTEGER NOT NULL DEFAULT 0,
    scene_id TEXT,
    job_id TEXT,
    safety_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    approved_by TEXT,
    approved_at TIMESTAMP WITH TIME ZONE,
    revoked_by TEXT,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revocation_reason TEXT,
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS character_canon_versions (
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    timeline_id TEXT NOT NULL DEFAULT 'main',
    canon_version INTEGER NOT NULL,
    snapshot JSONB NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (character_id, timeline_id, canon_version)
);

CREATE TABLE IF NOT EXISTS character_knowledge_policies (
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    policy_version INTEGER NOT NULL,
    allow_auto_approve_low BOOLEAN NOT NULL DEFAULT TRUE,
    allow_auto_approve_medium BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    shared_with_characters JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (character_id, policy_version)
);

CREATE TABLE IF NOT EXISTS character_knowledge_revocations (
    revocation_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    legal_block BOOLEAN NOT NULL DEFAULT FALSE,
    critical_block BOOLEAN NOT NULL DEFAULT FALSE,
    actor TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    cache_invalidated_at TIMESTAMP WITH TIME ZONE,
    index_rebuilt_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS character_knowledge_incidents (
    incident_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    revocation_id TEXT REFERENCES character_knowledge_revocations(revocation_id) ON DELETE SET NULL,
    severity TEXT NOT NULL DEFAULT 'critical',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_by TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS character_knowledge_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    brand_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    timeline_id TEXT NOT NULL DEFAULT 'main',
    canon_version INTEGER,
    item_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    snapshot_sha256 TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ck_sources_char ON character_knowledge_sources(character_id);
CREATE INDEX IF NOT EXISTS idx_ck_sources_sha ON character_knowledge_sources(character_id, sha256);
CREATE INDEX IF NOT EXISTS idx_ck_chunks_source ON character_knowledge_chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_ck_chunks_char ON character_knowledge_chunks(character_id);
CREATE INDEX IF NOT EXISTS idx_ck_items_char_status ON character_knowledge_items(character_id, status);
CREATE INDEX IF NOT EXISTS idx_ck_items_timeline ON character_knowledge_items(character_id, timeline_id, timeline_order);
CREATE INDEX IF NOT EXISTS idx_ck_revocations_target ON character_knowledge_revocations(character_id, target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_ck_snapshots_job ON character_knowledge_snapshots(job_id);
CREATE INDEX IF NOT EXISTS idx_ck_incidents_job ON character_knowledge_incidents(job_id);
