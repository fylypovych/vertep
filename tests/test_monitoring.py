import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_monitoring_role_has_metrics_logs_alerts_and_provisioning():
    roles = json.loads((ROOT / "config/node_roles.json").read_text(encoding="utf-8"))
    monitoring = roles["monitoring"]
    assert {"monitoring", "log-store", "log-collector", "grafana"} <= set(monitoring["services"])
    assert {"metrics", "logs", "alerting"} <= set(monitoring["capabilities"])
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro" not in compose
    assert "./monitoring/promtail.yml:/etc/promtail/config.yml:ro" not in compose
    assert "./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro" not in compose
    assert (ROOT / "docker/monitoring/Dockerfile").is_file()
    assert (ROOT / "docker/log-collector/Dockerfile").is_file()
    assert (ROOT / "docker/grafana/Dockerfile").is_file()


def test_monitoring_configuration_has_no_default_anonymous_grafana():
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "false"' in compose
    datasources = (ROOT / "monitoring/grafana/provisioning/datasources/vertep.yml").read_text(
        encoding="utf-8")
    assert "vertep-prometheus" in datasources
    assert "vertep-loki" in datasources
    dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/fleet.json").read_text(
        encoding="utf-8"))
    assert dashboard["uid"] == "vertep-fleet"
    assert {panel["type"] for panel in dashboard["panels"]} == {"stat", "logs"}


# ── Prometheus scrape inventory (Issue #53, #19) ──────────────────────────


def _load_prometheus_config():
    import yaml
    return yaml.safe_load((ROOT / "monitoring/prometheus.yml").read_text(encoding="utf-8"))


def _load_alerts_config():
    import yaml
    return yaml.safe_load((ROOT / "monitoring/alerts.yml").read_text(encoding="utf-8"))


def test_prometheus_scrape_includes_vertep_core():
    """CORE /metrics endpoint must be scraped for job/queue/worker metrics."""
    config = _load_prometheus_config()
    jobs = {c["job_name"]: c for c in config["scrape_configs"]}
    assert "vertep-core" in jobs
    core = jobs["vertep-core"]
    assert core.get("metrics_path") == "/metrics"
    targets = []
    for static in core.get("static_configs", []):
        targets.extend(static.get("targets", []))
    assert any("core" in t for t in targets)


def test_prometheus_scrape_includes_vertep_workers():
    """Worker nodes must be scraped for distributed metric collection."""
    config = _load_prometheus_config()
    jobs = {c["job_name"]: c for c in config["scrape_configs"]}
    assert "vertep-workers" in jobs
    workers = jobs["vertep-workers"]
    assert workers.get("metrics_path") == "/metrics"
    # Workers use DNS SRV discovery for dynamic membership
    assert workers.get("dns_sd_configs") or workers.get("static_configs")


def test_prometheus_scrape_includes_node_exporter():
    """System-level metrics from node-exporter must be available."""
    config = _load_prometheus_config()
    jobs = {c["job_name"]: c for c in config["scrape_configs"]}
    assert "node-exporter" in jobs


def test_prometheus_scrape_includes_loki():
    """Loki log-store metrics must be scraped."""
    config = _load_prometheus_config()
    jobs = {c["job_name"]: c for c in config["scrape_configs"]}
    assert "loki" in jobs


def test_prometheus_scrape_includes_prometheus_self():
    """Prometheus self-monitoring must be present."""
    config = _load_prometheus_config()
    jobs = {c["job_name"]: c for c in config["scrape_configs"]}
    assert "prometheus" in jobs


def test_alerts_include_job_failure_rules():
    """Job failure and dead-letter alerts must be defined."""
    alerts = _load_alerts_config()
    all_rules = {}
    for group in alerts["groups"]:
        for rule in group.get("rules", []):
            all_rules[rule["alert"]] = rule
    assert "VertepJobFailureSpike" in all_rules
    assert "VertepDeadLetterTasks" in all_rules
    # Dead-letter rule must reference the correct metric
    assert "vertep_queue_dead_letter" in all_rules["VertepDeadLetterTasks"]["expr"]


def test_alerts_include_worker_and_service_rules():
    """Worker offline, CORE down, and node-exporter down alerts must exist."""
    alerts = _load_alerts_config()
    all_rules = {}
    for group in alerts["groups"]:
        for rule in group.get("rules", []):
            all_rules[rule["alert"]] = rule
    assert "VertepWorkerOffline" in all_rules
    assert "VertepCoreServiceDown" in all_rules
    assert "VertepNodeExporterDown" in all_rules


def test_alerts_include_log_store_availability():
    """Loki unavailability alert must remain."""
    alerts = _load_alerts_config()
    all_rules = {}
    for group in alerts["groups"]:
        for rule in group.get("rules", []):
            all_rules[rule["alert"]] = rule
    assert "VertepLogStoreUnavailable" in all_rules
