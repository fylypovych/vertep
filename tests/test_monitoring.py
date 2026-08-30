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
