-- #503: history retention, 3 days by default; the administrator may extend it.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);
INSERT INTO settings (key, value) VALUES ('retention_days', '3') ON CONFLICT (key) DO NOTHING;
