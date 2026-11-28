"""Regression tests for Issue #553 AC6, AC7, and AC11.

- AC6: Production Compose & signed runtime bundle ship workflow definitions with
  ``WORKFLOW_DEFAULT_MODE=static`` so clean installs stay static, templates are
  available (``dynamic_available=True``), switching works and survives updates.
- AC7: ``worker`` and ``publisher-worker`` never inherit CORE secrets from ``.env``
  (no ``env_file: .env``, no ``REGISTRATION_TOKEN`` or other CORE secrets).
- AC11: Grafana SQLite uses WAL mode, serialized connections, and busy retries so
  dashboard provisioning is not lost to ``database is locked`` across restarts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import time
import urllib.request
import base64
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "docker-compose.yml"

FORBIDDEN_WORKER_SECRETS = (
    "POSTGRES_PASSWORD",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
    "SESSION_SECRET",
    "REGISTRATION_TOKEN",
    "GRAFANA_ADMIN_PASSWORD",
    "SECRET_STORE_PASSPHRASE",
    "ADMIN_PASSWORD",
    "REDIS_PASSWORD",
    "INTERNAL_API_KEY",
    "WORKER_SECRET",
    "NODE_API_TOKEN",
    "SETUP_TOKEN_HASH",
    "UPDATE_DATABASE_URL",
    "DATABASE_URL",
    "REDIS_URL",
)


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


# ── AC6: Dynamic workflow in production Compose and signed runtime bundle ────


def test_ac6_production_compose_delivers_workflow_definitions_with_static_default():
    compose = _load_compose()
    core = compose["services"]["core"]
    env = core["environment"]
    volumes = core["volumes"]

    assert env["WORKFLOW_DEFINITIONS_ROOT"] == "/data/workflow-definitions"
    assert env["WORKFLOW_DEFAULT_MODE"] == "static"
    assert "./workflow-definitions:/data/workflow-definitions:ro" in volumes

    template_path = ROOT / "deploy" / "workflow-definitions" / "standard.yaml"
    assert template_path.is_file()
    from core.workflow.dsl import parse_workflow
    parsed = parse_workflow(template_path.read_text(encoding="utf-8"))
    assert parsed["name"] == "Стандартний"


def test_ac6_runtime_bundle_ships_signed_standard_workflow_template(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    spec = importlib.util.spec_from_file_location(
        "build_runtime_bundle_553", ROOT / "scripts" / "build-runtime-bundle.py")
    builder = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(builder)

    assert builder.FILES["deploy/workflow-definitions/standard.yaml"] == "workflow-definitions/standard.yaml"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = tmp_path / "private.pem"
    private_key.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    roles = json.loads((ROOT / "config" / "node_roles.json").read_text(encoding="utf-8"))
    services = {
        service
        for definition in roles.values()
        if isinstance(definition, dict)
        for service in definition.get("services", [])
    }
    lock = tmp_path / "images.json"
    lock.write_text(json.dumps({
        name: {"reference": f"ghcr.io/fylypovych/vertep/{name}",
               "digest": "sha256:" + "b" * 64, "platforms": ["linux/amd64"]}
        for name in sorted(services)
    }), encoding="utf-8")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    bundle = builder.build(ROOT, tmp_path / "bundle", version, 1000, lock, private_key, "c" * 40)

    shipped = bundle / "workflow-definitions" / "standard.yaml"
    source = ROOT / "deploy" / "workflow-definitions" / "standard.yaml"
    assert shipped.is_file()
    assert shipped.read_bytes() == source.read_bytes()

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["files"]["workflow-definitions/standard.yaml"]["sha256"] == expected_sha

    sums = dict(
        line.split("  ", 1)[::-1]
        for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert sums["workflow-definitions/standard.yaml"] == expected_sha


def test_ac6_clean_install_stays_static_allows_switching_and_persists_choice(monkeypatch, tmp_path):
    from core.app import app
    from core.workflow import mode
    from core.workflow import runtime as runtime_module

    definitions_root = ROOT / "deploy" / "workflow-definitions"
    monkeypatch.setattr(mode, "config_root", lambda: tmp_path)
    monkeypatch.setenv("WORKFLOW_DEFINITIONS_ROOT", str(definitions_root))
    monkeypatch.setenv("WORKFLOW_DEFAULT_MODE", "static")
    monkeypatch.setenv("WORKFLOW_STATE_ROOT", str(tmp_path / "workflow-state"))
    monkeypatch.setattr(runtime_module, "_runtime", None)

    client = TestClient(app)

    # 1. Clean install: static by default, dynamic available (#420/1).
    state = client.get("/api/settings/workflow-mode")
    assert state.status_code == 200
    assert state.json() == {
        "mode": "static",
        "default": "static",
        "dynamic_available": True,
        "active_dynamic_jobs": 0,
    }
    assert runtime_module.workflow_runtime() is None

    # 2. Switch to dynamic: templates become available via API.
    switched = client.post("/api/settings/workflow-mode", json={"mode": "dynamic"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["mode"] == "dynamic"
    assert switched.json()["dynamic_available"] is True

    templates = client.get("/api/workflow-execution/templates")
    assert templates.status_code == 200, templates.text
    assert {"id": "standard", "name": "Стандартний"} in templates.json()

    # 3. Simulated restart / upgrade: stored operator choice is preserved even
    # when production Compose keeps WORKFLOW_DEFAULT_MODE=static.
    monkeypatch.setattr(runtime_module, "_runtime", None)
    after_restart = client.get("/api/settings/workflow-mode")
    assert after_restart.status_code == 200
    assert after_restart.json()["mode"] == "dynamic"
    assert after_restart.json()["dynamic_available"] is True
    assert client.get("/api/workflow-execution/templates").status_code == 200

    # 4. Switch back to static: choice persists across restart/upgrade.
    back = client.post("/api/settings/workflow-mode", json={"mode": "static"})
    assert back.status_code == 200, back.text
    assert back.json()["mode"] == "static"
    assert back.json()["dynamic_available"] is True
    monkeypatch.setattr(runtime_module, "_runtime", None)
    assert mode.workflow_mode_state()["mode"] == "static"
    assert runtime_module.workflow_runtime() is None


# ── AC7: Worker and Publisher Worker secret isolation ────────────────────────


def test_ac7_worker_and_publisher_worker_have_no_env_file_or_core_secrets_in_yaml():
    compose = _load_compose()
    for service_name in ("worker", "publisher-worker"):
        service = compose["services"][service_name]
        assert "env_file" not in service, f"{service_name} must not inherit shared .env via env_file"
        env = service.get("environment") or {}
        if isinstance(env, list):
            keys = {item.split("=", 1)[0] for item in env}
        else:
            keys = set(env.keys())
        leaked = sorted(keys & set(FORBIDDEN_WORKER_SECRETS))
        assert not leaked, f"{service_name} declares forbidden secrets: {leaked}"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI is required")
def test_ac7_docker_compose_config_excludes_core_secrets_from_workers(tmp_path):
    secret_values = {
        name: f"SENTINEL_{name}_SECRET_VALUE"
        for name in FORBIDDEN_WORKER_SECRETS
    }
    non_secret_env = {
        "WEB_DOMAIN": "vertep.test",
        "VERTEP_IMAGE_REPOSITORY": "ghcr.io/fylypovych/vertep",
        "VERTEP_VERSION": "0.0.4.68",
        "NODE_NAME": "test-node",
        "NODE_ROLE": "core",
        "NODE_ADDITIONAL_ROLES": "text,voice,publisher",
        "NODE_ENGINE_RUNTIMES": "moneyprinter",
        "CORE_URL": "",
        "DEMO_MODE": "false",
        "LOG_LEVEL": "INFO",
    }
    compose_copy = tmp_path / "docker-compose.yml"
    compose_copy.write_bytes(COMPOSE_PATH.read_bytes())
    env_file = tmp_path / ".env"
    env_lines = [f"{k}={v}" for k, v in {**non_secret_env, **secret_values}.items()]
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_copy), "config"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(result.stdout)

    # Sanity check: .env was genuinely loaded by Compose (core still receives it).
    core_env = rendered["services"]["core"]["environment"]
    assert core_env.get("JWT_SECRET") == secret_values["JWT_SECRET"]

    for service_name in ("worker", "publisher-worker"):
        service = rendered["services"][service_name]
        env = service.get("environment") or {}
        assert isinstance(env, dict), f"unexpected environment shape for {service_name}"

        # No forbidden secret keys.
        leaked_keys = sorted(set(env.keys()) & set(FORBIDDEN_WORKER_SECRETS))
        assert not leaked_keys, f"{service_name} leaked secret keys: {leaked_keys}"

        # No forbidden secret values interpolated into any other env var.
        rendered_values = "\n".join(str(v) for v in env.values() if v is not None)
        leaked_values = [
            name for name, sentinel in secret_values.items()
            if sentinel in rendered_values
        ]
        assert not leaked_values, f"{service_name} leaked secret values: {leaked_values}"

    # Verify required non-secret runtime variables remain intact.
    worker_env = rendered["services"]["worker"]["environment"]
    for required_key in (
        "CORE_ADDRESS",
        "NODE_NAME",
        "NODE_ROLE",
        "NODE_ADDITIONAL_ROLES",
        "NODE_ENGINE_RUNTIMES",
        "NODE_CONFIG_PATH",
        "WORKFLOWS_ROOT",
        "JOB_ROOT",
        "CHARACTERS_ROOT",
        "MODELS_ROOT",
        "VERTEP_VERSION",
        "VERTEP_APPLICATION_MODULES",
        "VERTEP_APPLICATION_STATE",
        "UPDATE_REQUEST_DIR",
        "TTS_HEALTH_URL",
        "PUBLISHER_HEALTH_URL",
        "PROMETHEUS_URL",
        "BACKUP_ROOT",
        "LOG_ROOT",
    ):
        assert required_key in worker_env, f"worker missing required env var {required_key}"
    assert worker_env["VERTEP_VERSION"] == "0.0.4.68"
    assert worker_env["NODE_NAME"] == "test-node"
    assert worker_env["NODE_ADDITIONAL_ROLES"] == "text,voice,publisher"
    assert worker_env["NODE_ENGINE_RUNTIMES"] == "moneyprinter"

    publisher_env = rendered["services"]["publisher-worker"]["environment"]
    assert publisher_env["LOG_ROOT"] == "/data/logs"
    assert publisher_env["JOB_ROOT"] == "/data/storage/jobs"
    assert publisher_env["VERTEP_VERSION"] == "0.0.4.68"


# ── AC11: Grafana SQLite WAL, retries, and provisioning stability ────────────


def test_ac11_grafana_compose_enables_sqlite_wal_serialized_connections_and_retries():
    compose = _load_compose()
    grafana = compose["services"]["grafana"]
    env = grafana["environment"]

    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert env["GF_DATABASE_TYPE"] == "sqlite3"
    assert str(env["GF_DATABASE_WAL"]).lower() == "true"
    assert int(env["GF_DATABASE_MAX_OPEN_CONN"]) == 1
    assert int(env["GF_DATABASE_MAX_IDLE_CONN"]) == 1
    assert int(env["GF_DATABASE_QUERY_RETRIES"]) >= 5
    assert int(env["GF_DATABASE_TRANSACTION_RETRIES"]) >= 5
    assert "grafana-data:/var/lib/grafana" in grafana["volumes"]


def test_ac11_grafana_provisioning_files_are_valid_and_resilient():
    dashboards_cfg = yaml.safe_load(
        (ROOT / "monitoring" / "grafana" / "provisioning" / "dashboards" / "vertep.yml")
        .read_text(encoding="utf-8")
    )
    assert dashboards_cfg["apiVersion"] == 1
    providers = dashboards_cfg["providers"]
    assert len(providers) >= 1
    vertep_provider = next(p for p in providers if p["name"] == "Vertep")
    assert vertep_provider["folder"] == "Vertep"
    assert vertep_provider["type"] == "file"
    assert vertep_provider["disableDeletion"] is True
    assert vertep_provider["editable"] is False
    assert vertep_provider["allowUiUpdates"] is False
    assert int(vertep_provider["updateIntervalSeconds"]) >= 10
    assert vertep_provider["options"]["path"] == "/var/lib/grafana/dashboards"

    datasources_cfg = yaml.safe_load(
        (ROOT / "monitoring" / "grafana" / "provisioning" / "datasources" / "vertep.yml")
        .read_text(encoding="utf-8")
    )
    assert datasources_cfg["apiVersion"] == 1
    uids = {ds["uid"] for ds in datasources_cfg["datasources"]}
    assert {"vertep-prometheus", "vertep-loki"} <= uids

    dashboard = json.loads(
        (ROOT / "monitoring" / "grafana" / "dashboards" / "fleet.json")
        .read_text(encoding="utf-8")
    )
    assert dashboard["uid"] == "vertep-fleet"


def _local_grafana_image() -> str | None:
    if shutil.which("docker") is None:
        return None
    for candidate in ("grafana/grafana:12.1.1",):
        probe = subprocess.run(
            ["docker", "image", "inspect", candidate],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return None


def test_ac11_grafana_container_restart_preserves_provisioned_dashboard_without_sqlite_lock(tmp_path):
    image = _local_grafana_image()
    if image is None:
        pytest.skip("local grafana/grafana:12.1.1 image is not cached")

    compose = _load_compose()
    grafana_env = compose["services"]["grafana"]["environment"]
    volume_name = f"vertep-ac11-grafana-{int(time.time())}"
    container_name = f"vertep-ac11-grafana-{int(time.time())}"
    admin_password = "VertepGrafanaTestPassword123!"

    def _wait_ready(port: int, timeout: float = 45.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.5)
        raise AssertionError("Grafana did not become healthy in time")

    def _fetch_dashboard(port: int) -> dict:
        token = base64.b64encode(f"admin:{admin_password}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/dashboards/uid/vertep-fleet",
            headers={"Authorization": f"Basic {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            return json.loads(resp.read().decode("utf-8"))

    try:
        subprocess.run(["docker", "volume", "create", volume_name], check=True, capture_output=True)
        env_args = [
            "-e", "GF_AUTH_ANONYMOUS_ENABLED=false",
            "-e", "GF_SECURITY_ADMIN_USER=admin",
            "-e", f"GF_SECURITY_ADMIN_PASSWORD={admin_password}",
            "-e", f"GF_DATABASE_TYPE={grafana_env['GF_DATABASE_TYPE']}",
            "-e", f"GF_DATABASE_WAL={grafana_env['GF_DATABASE_WAL']}",
            "-e", f"GF_DATABASE_MAX_OPEN_CONN={grafana_env['GF_DATABASE_MAX_OPEN_CONN']}",
            "-e", f"GF_DATABASE_MAX_IDLE_CONN={grafana_env['GF_DATABASE_MAX_IDLE_CONN']}",
            "-e", f"GF_DATABASE_QUERY_RETRIES={grafana_env['GF_DATABASE_QUERY_RETRIES']}",
            "-e", f"GF_DATABASE_TRANSACTION_RETRIES={grafana_env['GF_DATABASE_TRANSACTION_RETRIES']}",
        ]
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container_name,
                "-p", "127.0.0.1:0:3000",
                *env_args,
                "-v", f"{volume_name}:/var/lib/grafana",
                "-v", f"{ROOT / 'monitoring/grafana/provisioning'}:/etc/grafana/provisioning:ro",
                "-v", f"{ROOT / 'monitoring/grafana/dashboards'}:/var/lib/grafana/dashboards:ro",
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        port_out = subprocess.check_output(
            ["docker", "port", container_name, "3000/tcp"], text=True
        ).strip().splitlines()[0]
        port = int(port_out.rsplit(":", 1)[1])

        _wait_ready(port)
        first = _fetch_dashboard(port)
        assert first["dashboard"]["uid"] == "vertep-fleet"

        # Restart container against the same persistent SQLite volume.
        subprocess.run(["docker", "restart", container_name], check=True, capture_output=True)
        port_out = subprocess.check_output(
            ["docker", "port", container_name, "3000/tcp"], text=True
        ).strip().splitlines()[0]
        port = int(port_out.rsplit(":", 1)[1])

        _wait_ready(port)
        second = _fetch_dashboard(port)
        assert second["dashboard"]["uid"] == "vertep-fleet"

        logs = subprocess.run(
            ["docker", "logs", container_name], capture_output=True, text=True, check=True
        )
        combined = (logs.stdout or "") + "\n" + (logs.stderr or "")
        assert "database is locked" not in combined.lower(), combined
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", "-f", volume_name], capture_output=True, check=False)
