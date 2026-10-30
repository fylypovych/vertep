"""#357 AC1: actual pinned Prometheus, node-exporter and Loki, real CORE.

Run on a Docker Linux engine. Production scrape targets, labels, ports, rules
and queries are preserved; only evaluation/scrape intervals are shortened.
"""
import json
import os
import shutil
import subprocess
import uuid

import httpx
import pytest
import yaml

from tests.test_monitoring_browser_e2e import monitoring_core, ROOT, TOKEN, NODE
from tests.test_media_browser_e2e import _free_port, _wait_until
from tests.acceptance_provenance import acceptance_provenance


@pytest.fixture
def prometheus(monitoring_core, tmp_path):
    def unavailable(reason):
        if os.getenv("VERTEP_REQUIRE_MONITORING_SCRAPE") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    if not shutil.which("docker"):
        unavailable("Docker is unavailable; actual Prometheus/exporter scrape is required")
    check = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True)
    if check.returncode or check.stdout.strip() != "linux":
        unavailable("A disposable Docker Linux engine is required: " + check.stderr)
    name = "vertep-monitoring-" + uuid.uuid4().hex[:12]
    containers = []

    def docker(*args):
        result = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    # Forward unchanged core:8080 to the real host process, without replacing
    # metrics generation. This is a TCP proxy, not a metrics fixture/exporter.
    proxy = tmp_path / "proxy.py"
    proxy.write_text('''import socket, socketserver, threading, sys
def copy(source, target):
    try:
        while data := source.recv(65536): target.sendall(data)
    finally:
        try: target.shutdown(socket.SHUT_WR)
        except OSError: pass
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("host.docker.internal", int(sys.argv[1]))) as target:
            thread = threading.Thread(target=copy, args=(self.request,target))
            thread.start()
            copy(target,self.request)
            thread.join()
class Server(socketserver.ThreadingTCPServer): allow_reuse_address=True
Server(("0.0.0.0",8080), Handler).serve_forever()
''', encoding="utf-8")
    config = yaml.safe_load((ROOT / "monitoring/prometheus.yml").read_text(encoding="utf-8"))
    config["global"].update(scrape_interval="1s", evaluation_interval="1s")
    config_path = tmp_path / "prometheus.yml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    port = _free_port()
    prometheus_url = os.getenv("VERTEP_MONITORING_PROMETHEUS_URL", f"http://127.0.0.1:{port}")
    prometheus_publish = os.getenv(
        "VERTEP_MONITORING_PROMETHEUS_PUBLISH", f"127.0.0.1:{port}:9090",
    )
    host_gateway = os.getenv("VERTEP_MONITORING_HOST_GATEWAY", "host-gateway")
    docker("network", "create", name)
    def launch(alias, image, *args, options=(), files=()):
        container = name + "-" + alias
        containers.append(container)
        # ``docker`` may address a remote Linux daemon from a Windows runner.
        # A Windows temporary path is not a valid bind source there, so copy
        # disposable config files into the created container before it starts.
        # This keeps the real images and their production paths intact.
        docker("create", "--name", container, "--network", name,
               "--network-alias", alias, *options, image, *args)
        for source, destination in files:
            docker("cp", source, f"{container}:{destination}")
        docker("start", container)
        return container
    try:
        launch("core", "python:3.12-slim", "python", "/proxy.py", monitoring_core["port"],
               options=("--add-host", f"host.docker.internal:{host_gateway}"),
               files=((proxy, "/proxy.py"),))
        launch("node-exporter", "prom/node-exporter:v1.8.2")
        launch("log-store", "grafana/loki:3.5.2", "-config.file=/etc/loki/config.yml",
               files=((ROOT / "monitoring/loki.yml", "/etc/loki/config.yml"),))
        prom = launch("prometheus", "prom/prometheus:v3.5.0", "--config.file=/etc/prometheus/prometheus.yml",
                      options=("-p", prometheus_publish),
                      files=((config_path, "/etc/prometheus/prometheus.yml"),
                             (ROOT / "monitoring/alerts.yml", "/etc/prometheus/alerts.yml")))
        with httpx.Client(base_url=prometheus_url, timeout=10) as api:
            def ready():
                try:
                    return api.get("/-/ready").status_code == 200
                except httpx.HTTPError:
                    return False
            _wait_until(ready, message="Prometheus ready")
            yield api
    finally:
        for container in reversed(containers):
            # Only containers created by this disposable fixture are removed.
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=30)


def test_actual_targets_series_rules_and_dashboard(prometheus, monitoring_core):
    api = monitoring_core["api"]
    response = api.post("/api/workers/heartbeat", json={"node_name": NODE,
                        "role": "text", "status": "FREE", "capabilities": ["llm"]})
    assert response.status_code == 200, response.text
    def query(expression):
        response = prometheus.get("/api/v1/query", params={"query": expression})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "success", payload
        return payload["data"]["result"]
    def targets():
        response = prometheus.get("/api/v1/targets")
        response.raise_for_status()
        return response.json()["data"]["activeTargets"]
    _wait_until(lambda: len(targets()) == 4 and all(t["health"] == "up" for t in targets()),
                timeout=90, message="all actual scrape targets up")
    actual = {t["labels"]["job"]: t for t in targets()}
    assert actual["vertep-core"]["scrapeUrl"] == "http://core:8080/metrics"
    assert actual["vertep-core"]["labels"]["vertep_role"] == "core"
    assert actual["node-exporter"]["scrapeUrl"] == "http://node-exporter:9100/metrics"
    assert actual["loki"]["scrapeUrl"] == "http://log-store:3100/metrics"
    for metric in ("vertep_jobs_status", "vertep_queue_dead_letter", "vertep_queue_ready",
                   "vertep_workers_online", "vertep_workers_expected", "node_cpu_seconds_total",
                   "prometheus_rule_evaluation_failures_total"):
        assert query(metric), f"Actual scraped series is absent: {metric}"
    worker_query = f'vertep_worker_up{{node="{NODE}",role="text"}}'
    _wait_until(lambda: bool(query(worker_query)), message="registered worker actual series")
    worker = query(worker_query)
    assert len(worker) == 1
    assert query('vertep_jobs_status{status="FAILED"}')
    rules = yaml.safe_load((ROOT / "monitoring/alerts.yml").read_text(encoding="utf-8"))
    loaded = prometheus.get("/api/v1/rules").json()["data"]["groups"]
    assert {r["name"] for g in loaded for r in g["rules"]} == {
        r["alert"] for g in rules["groups"] for r in g["rules"]}
    for group in rules["groups"]:
        for rule in group["rules"]:
            query(rule["expr"])
    dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/fleet.json").read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        if panel["datasource"]["uid"] == "vertep-prometheus":
            for target in panel["targets"]:
                assert query(target["expr"])
    # Preserve actual metrics/API evidence in the test's artifact root.
    (monitoring_core["root"] / "scrape-evidence.json").write_text(
        json.dumps({"targets": targets(), "rules": loaded, "worker": worker}, indent=2), encoding="utf-8")
