CREATE SCHEMA IF NOT EXISTS web_ui;

CREATE TABLE IF NOT EXISTS web_ui.ui_config (
    key VARCHAR(128) PRIMARY KEY,
    value JSONB DEFAULT '{}',
    description TEXT DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS web_ui.ui_themes (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    colors JSONB DEFAULT '{}',
    is_default BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ui_themes_default ON web_ui.ui_themes(is_default) WHERE is_default = true;

CREATE TABLE IF NOT EXISTS web_ui.ui_navigation (
    id VARCHAR(64) PRIMARY KEY,
    label VARCHAR(128) NOT NULL,
    path VARCHAR(256) NOT NULL,
    icon VARCHAR(64) NOT NULL,
    `order` INT NOT NULL DEFAULT 0,
    module_namespace VARCHAR(64),
    required_permissions JSONB DEFAULT '[]',
    visible BOOLEAN DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_ui_nav_order ON web_ui.ui_navigation(`order`);
CREATE INDEX IF NOT EXISTS idx_ui_nav_module ON web_ui.ui_navigation(module_namespace);