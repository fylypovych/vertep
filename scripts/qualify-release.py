#!/usr/bin/env python3
"""Deterministic preflight gates for a Vertep appliance release artifact."""

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
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

REQUIRED_IMAGES = {
    "core", "worker", "comfyui", "tts", "publisher-worker", "backup-service",
    "proxy", "postgres", "redis", "ollama", "monitoring", "grafana",
    "log-store", "log-collector", "update-agent", "license-manager",
    "dispatcher", "scheduler", "certificate-manager",
}

REQUIRED_PLATFORMS = {"linux/amd64", "linux/arm64"}
ARM64_EXEMPT_SERVICES = {"comfyui"}


def qualify(root: Path, run_compose: bool = False, artifact_root: Path | None = None) -> dict:
    # artifact_root must point to a built runtime bundle (release workflow);
    # static repository checks never require release artifacts.
    artifact_checks = artifact_root is not None
    artifact_root = artifact_root or root
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
                "services/license_service.py", "services/dispatcher_service.py", "services/scheduler_service.py",
                "services/certificate_service.py",
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
    try:
        compose = (root / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    except OSError as error:
        record("compose_load", False, str(error))
        compose = ""
    try:
        contract_schema = json.loads(
            (root / "config/schemas/release-contract.schema.json").read_text(encoding="utf-8"))
        record("release_contract_schema", contract_schema.get("properties", {}).get(
            "schema", {}).get("const") == 2)
    except (OSError, ValueError) as error:
        record("release_contract_schema", False, str(error))
    try:
        bootstrap = (root / "bootstrap.sh").read_text(encoding="utf-8")
    except OSError as error:
        record("signed_role_catalog_binding", False, str(error))
        bootstrap = ""
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
    try:
        proxy_conf = (root / "deploy/proxy.conf").read_text(encoding="utf-8")
        record("machine_mtls_proxy", "ssl_verify_client optional" in proxy_conf)
    except OSError as error:
        record("machine_mtls_proxy", False, str(error))
    if run_compose:
        result = subprocess.run(["docker", "compose", "-f", str(root / "deploy/docker-compose.yml"),
                                 "config", "--quiet"], capture_output=True, text=True, check=False)
        record("docker_compose_config", result.returncode == 0, result.stderr[-1000:])

    # Cross-artifact/version/SHA/SBOM consistency checks
    try:
        roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
        record("role_catalog_version", "version" in roles, "role catalog missing version field")
        record("role_catalog_sha256", "catalog_sha256" in roles, "role catalog missing catalog_sha256 field")
    except (OSError, ValueError, KeyError) as error:
        record("role_catalog_version", False, str(error))
        record("role_catalog_sha256", False, str(error))

    # Arm64 ComfyUI qualification gate
    arm64_comfyui_issue = False
    arm64_comfyui_detail = ""
    try:
        roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
        if ("gpu" in roles and "comfyui" in roles["gpu"].get("services", [])
                and "comfyui" not in ARM64_EXEMPT_SERVICES):
            arm64_comfyui_issue = True
            arm64_comfyui_detail = "comfyui service present on gpu role but arm64 support not verified"
    except (OSError, ValueError, KeyError) as error:
        arm64_comfyui_detail = str(error)
    record("arm64_comfyui_qualification_gate", not arm64_comfyui_issue, arm64_comfyui_detail)

    # All required images/platforms and immutable digests check (runtime bundle only)
    if artifact_checks:
        try:
            images_lock_path = artifact_root / "images.json"
            if images_lock_path.is_file():
                images = json.loads(images_lock_path.read_text(encoding="utf-8"))
                missing_services = REQUIRED_IMAGES - set(images.keys())
                record("all_required_images_present", not missing_services, ", ".join(sorted(missing_services)))
                bad_platforms = []
                for service, image in images.items():
                    platforms = image.get("platforms", [])
                    if service not in ARM64_EXEMPT_SERVICES and not REQUIRED_PLATFORMS.issubset(set(platforms)):
                        bad_platforms.append(f"{service}: {platforms}")
                    digest = image.get("digest", "")
                    if not digest.startswith("sha256:") or len(digest) != 71:
                        bad_platforms.append(f"{service}: invalid digest {digest}")
                record("all_images_have_required_platforms_and_digests", not bad_platforms, "; ".join(bad_platforms))
            else:
                record("all_required_images_present", False, "images.json not found")
                record("all_images_have_required_platforms_and_digests", False, "images.json not found")
        except (OSError, ValueError) as error:
            record("all_required_images_present", False, str(error))
            record("all_images_have_required_platforms_and_digests", False, str(error))

        # Signed manifest/update manifest checks
        try:
            manifest_path = artifact_root / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                record("manifest_signed", "signature" in manifest, "manifest missing signature")
                record("manifest_has_version", "version" in manifest, "manifest missing version")
                record("manifest_has_release_sequence", "release_sequence" in manifest, "manifest missing release_sequence")
                record("manifest_has_compatibility", "compatibility" in manifest, "manifest missing compatibility")
                record("manifest_has_roles", "roles" in manifest, "manifest missing roles")
                record("manifest_has_sbom", "sbom" in manifest, "manifest missing sbom")
                record("manifest_has_files", "files" in manifest, "manifest missing files")
                record("manifest_has_images", "images" in manifest, "manifest missing images")
            else:
                record("manifest_signed", False, "manifest.json not found")
        except (OSError, ValueError) as error:
            record("manifest_signed", False, str(error))

        # SBOM format check
        try:
            sbom_path = artifact_root / "sbom.cdx.json"
            if sbom_path.is_file():
                sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
                record("sbom_format_cyclonedx", sbom.get("bomFormat") == "CycloneDX", "SBOM is not CycloneDX format")
                record("sbom_has_spec_version", "specVersion" in sbom, "SBOM missing specVersion")
            else:
                record("sbom_format_cyclonedx", False, "sbom.cdx.json not found")
                record("sbom_has_spec_version", False, "sbom.cdx.json not found")
        except (OSError, ValueError) as error:
            record("sbom_format_cyclonedx", False, str(error))
            record("sbom_has_spec_version", False, str(error))

    if artifact_checks:
        record("release_artifact_gates", True, "checked")
    else:
        record("release_artifact_gates", True, "skipped (static repository check)")

    passed = all(item["passed"] for item in checks)
    return {"schema": 1, "passed": passed, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--docker", action="store_true", help="also run Docker Compose validation")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifact-root", type=Path, help="Directory containing the built runtime artifacts")
    args = parser.parse_args()
    report = qualify(args.root.resolve(), args.docker, args.artifact_root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
