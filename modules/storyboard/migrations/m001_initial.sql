-- Storyboard module migration m001_initial (version 1.0.0)
-- Scenario Library (#508): independent Scenario Documents, scenes, drafts,
-- immutable versions, inter-brand sharing and access audit inside mod_storyboard.

CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id TEXT PRIMARY KEY,
    owner_brand_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    draft_title TEXT NOT NULL,
    draft_description TEXT NOT NULL DEFAULT '',
    draft_scenes JSONB NOT NULL DEFAULT '[]'::jsonb,
    draft_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    draft_updated_by TEXT NOT NULL DEFAULT '',
    latest_version INTEGER NOT NULL DEFAULT 0 CHECK (latest_version >= 0),
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    archived_at TIMESTAMPTZ,
    archived_by TEXT,
    copied_from_scenario_id TEXT,
    copied_from_version INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scenario_versions (
    scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    scenes JSONB NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scenario_id, version)
);

CREATE TABLE IF NOT EXISTS scenario_shares (
    scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    brand_id TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by TEXT NOT NULL,
    PRIMARY KEY (scenario_id, brand_id)
);

CREATE TABLE IF NOT EXISTS scenario_audit (
    id BIGSERIAL PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    brand_id TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scenarios_owner ON scenarios(owner_brand_id);
CREATE INDEX IF NOT EXISTS idx_scenarios_archived ON scenarios(archived);
CREATE INDEX IF NOT EXISTS idx_scenario_shares_brand ON scenario_shares(brand_id);
CREATE INDEX IF NOT EXISTS idx_scenario_audit_scenario ON scenario_audit(scenario_id);
CREATE INDEX IF NOT EXISTS idx_scenario_audit_at ON scenario_audit(at);
