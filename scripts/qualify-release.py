#!/usr/bin/env python3
"""Deterministic preflight gates for a Vertep appliance release artifact."""

import argparse
import json
import subprocess
from pathlib import Path


FORBIDDEN_BY_ROLE = {
    "core": {"worker", "comfyui", "tts", "publisher-worker", "grafana"},
    "gpu": {"core", "migrate", "postgres", "redis", "ollama", "tts"},
    "text": {"core", "migrate", "postgres", "redis", "comfyui", "tts"},
    "voice": {"core", "migrate", "postgres", "redis", "comfyui", "ollama"},
    "publisher": {"core", "migrate", "postgres", "redis", "comfyui", "ollama", "tts"},
    "backup": {"core", "migrate", "postgres", "redis", "comfyui", "ollama", "tts"},
    "monitoring": {"core", "migrate", "postgres", "redis", "comfyui", "ollama", "tts"},
}


def qualify(root: Path, run_compose: bool = False) -> dict:
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    required = ["bootstrap.sh", "deploy/docker-compose.yml", "deploy/docker-compose.amd.yml",
                "deploy/docker-compose.nvidia.yml", "config/node_roles.json",
                "config/schemas/release-contract.schema.json", "scripts/runtime-contract.py",
                "scripts/generate-sbom.py",
                "scripts/apply-deployment.py", "installer/vertep-deployment.service",
                "installer/vertep-deployment.path",
                "services/tts_service.py", "services/publisher_service.py", "services/backup_service.py",
                "services/license_service.py", "services/dispatcher_service.py",
                "services/scheduler_service.py", "services/certificate_service.py",
                "docker/tts/Dockerfile", "docker/publisher/Dockerfile", "docker/backup/Dockerfile",
                "docker/proxy/Dockerfile", "docker/proxy/entrypoint.sh", "docker/monitoring/Dockerfile",
                "docker/log-store/Dockerfile", "docker/log-collector/Dockerfile",
                "docker/grafana/Dockerfile",
                "monitoring/prometheus.yml", "monitoring/alerts.yml", "monitoring/loki.yml",
                "monitoring/promtail.yml", "monitoring/grafana/provisioning/datasources/vertep.yml",
                "monitoring/grafana/provisioning/dashboards/vertep.yml",
                "monitoring/grafana/dashboards/fleet.json",
                "scripts/update-agent.py", "scripts/release-layout.py", "installer/update-public.pem"]
    missing = [name for name in required if not (root / name).is_file()]
    record("required_release_files", not missing, ", ".join(missing))
    try:
        roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
        for role, forbidden in FORBIDDEN_BY_ROLE.items():
            services = set(roles[role]["services"])
            unexpected = sorted(services & forbidden)
            record(f"role_isolation:{role}", not unexpected, ", ".join(unexpected))
    except (OSError, ValueError, KeyError, TypeError) as error:
        record("role_catalog", False, str(error))
    compose = (root / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    try:
        contract_schema = json.loads(
            (root / "config/schemas/release-contract.schema.json").read_text(encoding="utf-8"))
        record("release_contract_schema", contract_schema.get("properties", {}).get(
            "schema", {}).get("const") == 2)
    except (OSError, ValueError) as error:
        record("release_contract_schema", False, str(error))
    bootstrap = (root / "bootstrap.sh").read_text(encoding="utf-8")
    record("signed_role_catalog_binding",
           ".roles.catalog_sha256" in bootstrap and ".roles.profiles[$role].services" in bootstrap)
    image_variables = {"VERTEP_PROXY_IMAGE", "VERTEP_CORE_IMAGE", "VERTEP_WORKER_IMAGE",
                       "VERTEP_COMFYUI_IMAGE", "VERTEP_TTS_IMAGE", "VERTEP_PUBLISHER_WORKER_IMAGE",
                       "VERTEP_BACKUP_SERVICE_IMAGE", "VERTEP_POSTGRES_IMAGE", "VERTEP_REDIS_IMAGE",
                       "VERTEP_OLLAMA_IMAGE", "VERTEP_MONITORING_IMAGE", "VERTEP_GRAFANA_IMAGE",
                       "VERTEP_LOG_STORE_IMAGE", "VERTEP_LOG_COLLECTOR_IMAGE",
                       "VERTEP_UPDATE_AGENT_IMAGE", "VERTEP_LICENSE_MANAGER_IMAGE",
                       "VERTEP_DISPATCHER_IMAGE", "VERTEP_SCHEDULER_IMAGE",
                       "VERTEP_CERTIFICATE_MANAGER_IMAGE"}
    missing_image_variables = sorted(name for name in image_variables if f"${{{name}" not in compose)
    record("compose_image_digest_overrides", not missing_image_variables,
           ", ".join(missing_image_variables))
    record("no_docker_socket", "/var/run/docker.sock" not in compose)
    mutable_runtime_mounts = ["./runtime/proxy.conf", "./monitoring/prometheus.yml",
                              "./monitoring/loki.yml", "./monitoring/promtail.yml",
                              "./monitoring/grafana/provisioning"]
    record("immutable_runtime_configuration",
           not any(item in compose for item in mutable_runtime_mounts))
    record("machine_mtls_proxy", "ssl_verify_client optional" in
           (root / "deploy/proxy.conf").read_text(encoding="utf-8"))
    if run_compose:
        result = subprocess.run(["docker", "compose", "-f", str(root / "deploy/docker-compose.yml"),
                                 "config", "--quiet"], capture_output=True, text=True, check=False)
        record("docker_compose_config", result.returncode == 0, result.stderr[-1000:])
    passed = all(item["passed"] for item in checks)
    return {"schema": 1, "passed": passed, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--docker", action="store_true", help="also run Docker Compose validation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = qualify(args.root.resolve(), args.docker)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
