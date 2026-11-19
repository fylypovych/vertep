
CREATE TABLE IF NOT EXISTS metrics (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    labels JSONB DEFAULT '{}',
    timestamp TIMESTAMPTZ NOT NULL,
    node_id VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name);
CREATE INDEX IF NOT EXISTS idx_metrics_node ON metrics(node_id);
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_name_node_time ON metrics(name, node_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    condition TEXT NOT NULL,
    message TEXT DEFAULT '',
    active BOOLEAN DEFAULT false,
    fired_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    labels JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_fired ON alerts(fired_at DESC);

CREATE TABLE IF NOT EXISTS health_checks (
    id VARCHAR(64) PRIMARY KEY,
    component VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('healthy', 'degraded', 'unhealthy')),
    message TEXT DEFAULT '',
    details JSONB DEFAULT '{}',
    checked_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_component ON health_checks(component);
CREATE INDEX IF NOT EXISTS idx_health_checked ON health_checks(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_status ON health_checks(status);

CREATE TABLE IF NOT EXISTS system_logs (
    id BIGSERIAL PRIMARY KEY,
    level VARCHAR(16) NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
    component VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    context JSONB DEFAULT '{}',
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_level ON system_logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_component ON system_logs(component);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON system_logs(timestamp DESC);