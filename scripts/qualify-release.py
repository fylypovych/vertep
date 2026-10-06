#!/usr/bin/env python3
"""Deterministic preflight gates for a Vertep appliance release artifact."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.release_contract import validate_release_contract


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
    "dispatcher", "scheduler", "certificate-manager", "moneyprinter",
    "node-exporter",
}

REQUIRED_PLATFORMS = {"linux/amd64", "linux/arm64"}
# moneyprinter ships linux/amd64 only: its dependency lock is resolved against
# the pinned upstream commit for that platform and is not qualified for arm64 yet.
# The service is opt-in, so an arm64-only install simply leaves the profile off.
ARM64_EXEMPT_SERVICES = {"comfyui", "moneyprinter"}

# Issue #122 P2: the isolated MoneyPrinterTurbo runtime must never become part of
# a Native install. A role that pulls it in would make the appliance depend on an
# optional external engine, so the dependency is rejected explicitly.
FORBIDDEN_IN_ANY_ROLE = {"moneyprinter"}

EXPECTED_CHANNEL = "stable"

CONTRACT_CHECKS = (
    "manifest_contract_structure",
    "contract_in_validity_window",
    "artifact_digests_recomputed",
    "role_catalog_bound",
    "sbom_bound",
    "manifest_signature_verified",
)


def _issued_reference(contract: dict) -> datetime:
    try:
        issued = datetime.fromisoformat(str(contract.get("issued_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if issued.tzinfo is None:
        return datetime.now(timezone.utc)
    return issued.astimezone(timezone.utc)


def _classify_contract_failure(error: Exception) -> str:
    text = str(error)
    if "validity window" in text:
        return "contract_in_validity_window"
    if "digest mismatch" in text or "wrong size" in text:
        return "artifact_digests_recomputed"
    if "Role catalog" in text or "role inventory" in text or "role profile" in text:
        return "role_catalog_bound"
    if "SBOM" in text:
        return "sbom_bound"
    if "signature verification failed" in text:
        return "manifest_signature_verified"
    return "manifest_contract_structure"


def _expected_release_sequence(version: str | None) -> int | None:
    if not version:
        return None
    parts = version.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return None
    p0, p1, p2, p3 = (int(part) for part in parts)
    return ((p0 * 100 + p1) * 100 + p2) * 100 + p3


def _record_contract_checks(record, contract: dict, artifact_root: Path,
                            public_key: Path) -> None:
    reference = _issued_reference(contract)
    try:
        validate_release_contract(contract, now=reference)
    except (ValueError, RuntimeError) as error:
        record("manifest_contract_structure", False, str(error))
        for name in CONTRACT_CHECKS[1:]:
            record(name, False, f"blocked: {error}")
        return
    record("manifest_contract_structure", True)
    try:
        validate_release_contract(contract)
    except RuntimeError as error:
        record("contract_in_validity_window", False, str(error))
        for name in CONTRACT_CHECKS[2:]:
            record(name, False, f"blocked: {error}")
        return
    record("contract_in_validity_window", True)
    try:
        validate_release_contract(contract, artifact_root=artifact_root, now=reference)
    except (ValueError, RuntimeError) as error:
        failed = _classify_contract_failure(error)
        if failed not in CONTRACT_CHECKS[2:]:
            record("release_artifact_binding", False, str(error))
            for name in CONTRACT_CHECKS[2:]:
                record(name, False, f"blocked: {error}")
            return
        index = CONTRACT_CHECKS.index(failed)
        for name in CONTRACT_CHECKS[2:index]:
            record(name, True)
        record(failed, False, str(error))
        for name in CONTRACT_CHECKS[index + 1:]:
            record(name, False, f"blocked: {error}")
        return
    record("artifact_digests_recomputed", True)
    record("role_catalog_bound", True)
    record("sbom_bound", True)
    try:
        validate_release_contract(contract, artifact_root=artifact_root,
                                  public_key=public_key, now=reference)
    except (ValueError, RuntimeError) as error:
        record("manifest_signature_verified", False, str(error))
        return
    record("manifest_signature_verified", True)


def _record_cross_artifact_checks(record, contract: dict, artifact_root: Path,
                                  root: Path) -> None:
    try:
        repo_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as error:
        repo_version = None
        record("manifest_version_matches_version_file", False, str(error))
    else:
        problems = []
        if contract.get("version") != repo_version:
            problems.append(f"manifest={contract.get('version')} VERSION={repo_version}")
        bundle_version = artifact_root / "VERSION"
        if bundle_version.is_file():
            try:
                content = bundle_version.read_text(encoding="utf-8").strip()
            except OSError as error:
                problems.append(str(error))
            else:
                if content != contract.get("version"):
                    problems.append(f"bundle={content} manifest={contract.get('version')}")
        record("manifest_version_matches_version_file", not problems, "; ".join(problems))
    try:
        images_lock = json.loads((artifact_root / "images.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        images_lock = None
        record("manifest_images_match_images_lock", False, str(error))
        record("image_digests_match_manifest", False, str(error))
    if isinstance(images_lock, dict):
        record("manifest_images_match_images_lock", contract.get("images") == images_lock,
               "" if contract.get("images") == images_lock else "manifest images differ from images.json")
        manifest_images = contract.get("images") if isinstance(contract.get("images"), dict) else {}
        mismatched = sorted(
            service for service, image in images_lock.items()
            if not isinstance(image, dict) or not isinstance(manifest_images.get(service), dict)
            or manifest_images[service].get("digest") != image.get("digest"))
        record("image_digests_match_manifest", not mismatched, ", ".join(mismatched))
    elif images_lock is not None:
        record("manifest_images_match_images_lock", False, "images.json is not an object")
        record("image_digests_match_manifest", False, "images.json is not an object")
    sbom_meta = contract.get("sbom") if isinstance(contract.get("sbom"), dict) else {}
    sbom_path = artifact_root / "sbom.cdx.json"
    if not sbom_path.is_file():
        record("manifest_sbom_hash_matches_sbom", False, "sbom.cdx.json not found")
    else:
        recomputed = hashlib.sha256(sbom_path.read_bytes()).hexdigest()
        record("manifest_sbom_hash_matches_sbom", sbom_meta.get("sha256") == recomputed,
               f"manifest={sbom_meta.get('sha256')} actual={recomputed}")
    record("manifest_channel_is_stable", contract.get("channel") == EXPECTED_CHANNEL,
           str(contract.get("channel")))
    expected_sequence = _expected_release_sequence(repo_version)
    if expected_sequence is None:
        record("manifest_release_sequence_matches_version", False,
               f"cannot derive sequence from VERSION {repo_version!r}")
    else:
        record("manifest_release_sequence_matches_version",
               contract.get("release_sequence") == expected_sequence,
               f"manifest={contract.get('release_sequence')} expected={expected_sequence}")


def _compose_service(compose: str, name: str) -> str:
    """Return the raw YAML block of one compose service, or an empty string."""
    lines = compose.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == f"  {name}:":
            start = index
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _locked_config_values(text: str, section: str) -> dict:
    """Read ``key = value`` pairs of one TOML section without a TOML dependency.

    The release gate must run on the same interpreters as the rest of the toolchain,
    so it cannot assume ``tomllib`` or ``toml`` is importable. Only the shapes this
    lock uses are supported: booleans, quoted strings and empty inline lists.
    """
    values: dict[str, object] = {}
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            current = stripped.strip("[]")
            continue
        if current != section or "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, raw = stripped.partition("=")
        raw = raw.strip()
        if raw in ("true", "false"):
            values[key.strip()] = raw == "true"
        elif raw.startswith('"') and raw.endswith('"'):
            values[key.strip()] = raw[1:-1]
        elif raw == "[]":
            values[key.strip()] = []
        else:
            values[key.strip()] = raw
    return values


def qualify(root: Path, run_compose: bool = False, artifact_root: Path | None = None,
            public_key: Path | None = None) -> dict:
    # artifact_root must point to a built runtime bundle (release workflow);
    # static repository checks never require release artifacts.
    artifact_checks = artifact_root is not None
    artifact_root = artifact_root or root
    trust_key = public_key or (root / "installer/update-public.pem")
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
                "docker/grafana/Dockerfile", "docker/node-exporter/Dockerfile",
                "docker/moneyprinter/Dockerfile", "docker/moneyprinter/entrypoint.sh",
                "docker/moneyprinter/requirements.lock", "docker/moneyprinter/config.lock.toml",
                "services/moneyprinter_service.py",
                "adapters/providers/runtime_manifest.py",
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
        optional_leaks = sorted({
            f"{role}:{service}"
            for role, definition in roles.items()
            if isinstance(definition, dict) and isinstance(definition.get("services"), list)
            for service in definition["services"]
            if service in FORBIDDEN_IN_ANY_ROLE
        })
        record("optional_engine_stays_out_of_roles", not optional_leaks,
               ", ".join(optional_leaks))
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
                       "VERTEP_CERTIFICATE_MANAGER_IMAGE", "VERTEP_MONEYPRINTER_IMAGE"}
    missing_image_variables = sorted(name for name in image_variables if f"${{{name}" not in compose)
    record("compose_image_digest_overrides", not missing_image_variables,
           ", ".join(missing_image_variables))
    record("no_docker_socket", "/var/run/docker.sock" not in compose)

    # --- Issue #122 P2: the pinned MoneyPrinterTurbo runtime stays optional ---
    moneyprinter_block = _compose_service(compose, "moneyprinter")
    if not moneyprinter_block:
        record("moneyprinter_opt_in", False, "moneyprinter service is missing from compose")
    else:
        opt_in = "profiles:" in moneyprinter_block and "moneyprinter" in moneyprinter_block
        unpublished = not re.search(r"^\s{4}ports:", moneyprinter_block, re.MULTILINE)
        record("moneyprinter_opt_in", bool(opt_in and unpublished),
               "profiles" if not opt_in else ("published port" if not unpublished else ""))
    try:
        lock_text = (root / "docker/moneyprinter/config.lock.toml").read_text(encoding="utf-8")
        config_lock = _locked_config_values(lock_text, "app")
        root_lock = _locked_config_values(lock_text, "")
        upload_locked = all(
            not config_lock.get(key)
            for key in ("upload_post_enabled", "upload_post_api_key",
                        "upload_post_username", "upload_post_auto_upload")
        ) and config_lock.get("upload_post_platforms") == []
        record("moneyprinter_auto_upload_disabled", upload_locked)
        record("moneyprinter_upstream_isolated",
               root_lock.get("listen_host") == "127.0.0.1"
               and config_lock.get("material_directory") == "task"
               and not config_lock.get("enable_redis"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        record("moneyprinter_auto_upload_disabled", False, str(error))
        record("moneyprinter_upstream_isolated", False, str(error))
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
        artifact_start = len(checks)
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
        contract = None
        try:
            manifest_path = artifact_root / "manifest.json"
            if manifest_path.is_file():
                contract = json.loads(manifest_path.read_text(encoding="utf-8"))
            else:
                record("manifest_signed", False, "manifest.json not found")
        except (OSError, ValueError) as error:
            record("manifest_signed", False, str(error))
            contract = None
        if isinstance(contract, dict):
            record("manifest_signed", "signature" in contract, "manifest missing signature")
            record("manifest_has_version", "version" in contract, "manifest missing version")
            record("manifest_has_release_sequence", "release_sequence" in contract, "manifest missing release_sequence")
            record("manifest_has_compatibility", "compatibility" in contract, "manifest missing compatibility")
            record("manifest_has_roles", "roles" in contract, "manifest missing roles")
            record("manifest_has_sbom", "sbom" in contract, "manifest missing sbom")
            record("manifest_has_files", "files" in contract, "manifest missing files")
            record("manifest_has_images", "images" in contract, "manifest missing images")
            try:
                _record_contract_checks(record, contract, artifact_root, trust_key)
            except Exception as error:
                record("release_artifact_contract", False, str(error))
            try:
                _record_cross_artifact_checks(record, contract, artifact_root, root)
            except Exception as error:
                record("release_artifact_cross_checks", False, str(error))
        else:
            if contract is not None:
                record("manifest_signed", False, "manifest.json is not an object")
            for name in CONTRACT_CHECKS:
                record(name, False, "manifest.json unavailable")

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
        failed = [item for item in checks[artifact_start:] if not item["passed"]]
        record("release_artifact_gates", not failed,
               "checked" if not failed else ", ".join(sorted({item["name"] for item in failed})))
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
