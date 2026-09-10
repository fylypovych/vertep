CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    target TEXT NOT NULL,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_channels_brand ON channels(brand_id);
