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
    # The node-exporter scrape job must have a real backing service, not a dangling
    # reference: it is part of the monitoring role catalog and the Compose file.
    assert "node-exporter" in monitoring["services"]
    assert (ROOT / "docker/node-exporter/Dockerfile").is_file()
    assert "VERTEP_NODE_EXPORTER_IMAGE" in compose


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
    # CORE is served by uvicorn over plain HTTP on 8080 (Dockerfile CMD); 8443 is
    # the TLS reverse proxy, not the `core` container.
    assert core.get("scheme", "http") == "http"
    assert "tls_config" not in core
    targets = []
    for static in core.get("static_configs", []):
        targets.extend(static.get("targets", []))
    assert "core:8080" in targets


def test_prometheus_worker_metrics_come_from_core_aggregation():
    """Worker nodes are outbound-only (Issue #80) and expose no scrape endpoint.

    CORE aggregates their state and re-exports vertep_worker_up /
    vertep_workers_expected on /metrics, so no direct worker scrape job exists.
    """
    config = _load_prometheus_config()
    jobs = {c["job_name"]: c for c in config["scrape_configs"]}
    assert "vertep-workers" not in jobs


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
