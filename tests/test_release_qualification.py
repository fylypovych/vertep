import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.release_contract import sign_release_contract


ROOT = Path(__file__).parents[1]


def module():
    path = ROOT / "scripts" / "qualify-release.py"
    spec = importlib.util.spec_from_file_location("qualify_release", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def contract_builder():
    path = ROOT / "scripts" / "runtime-contract.py"
    spec = importlib.util.spec_from_file_location("runtime_contract", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def keypair(tmp_path, name):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / f"{name}-private.pem"
    public_path = tmp_path / f"{name}-public.pem"
    private_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private_path, public_path


def artifact_fixture(tmp_path):
    bundle = tmp_path / "runtime"
    (bundle / "config").mkdir(parents=True)
    catalog_text = (ROOT / "config/node_roles.json").read_text(encoding="utf-8")
    (bundle / "config/node_roles.json").write_text(catalog_text, encoding="utf-8")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    (bundle / "VERSION").write_text(version + "\n", encoding="utf-8")
    (bundle / "sbom.cdx.json").write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}), encoding="utf-8")
    (bundle / "docker-compose.yml").write_text("name: vertep\n", encoding="utf-8")
    catalog = json.loads(catalog_text)
    services = {service for definition in catalog.values()
                if isinstance(definition, dict) and isinstance(definition.get("services"), list)
                for service in definition["services"]}
    names = sorted(set(module().REQUIRED_IMAGES) | services)
    images = {name: {"reference": f"registry.vertep.ai/vertep/{name}",
                     "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
                     "platforms": ["linux/amd64", "linux/arm64"]}
              for name in names}
    (bundle / "images.json").write_text(json.dumps(images), encoding="utf-8")
    # Published SHA256SUMS is part of the full artifact set.
    (bundle / "SHA256SUMS").write_text(
        f"{hashlib.sha256((bundle / 'VERSION').read_bytes()).hexdigest()}  VERSION\n"
        f"{hashlib.sha256((bundle / 'sbom.cdx.json').read_bytes()).hexdigest()}  sbom.cdx.json\n",
        encoding="utf-8")
    parts = [int(part) for part in version.split(".")]
    sequence = ((parts[0] * 100 + parts[1]) * 100 + parts[2]) * 100 + parts[3]
    private_path, public_path = keypair(tmp_path, "release")
    return bundle, private_path, public_path, version, sequence, images


def test_runtime_builder_emits_complete_recomputed_checksums(tmp_path):
    _, private_key, _, version, sequence, images = artifact_fixture(tmp_path)
    lock = tmp_path / "image-lock.json"
    lock.write_text(json.dumps(images), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "runtime_builder", ROOT / "scripts/build-runtime-bundle.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    output = builder.build(ROOT, tmp_path / "built", version, sequence,
                           lock, private_key, "c" * 40)
    sums = dict(line.split("  ", 1)[::-1] for line in
                (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines())
    actual = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in output.rglob("*") if path.is_file() and path.name != "SHA256SUMS"}
    assert sums == actual
    assert {"manifest.json", "images.json", "sbom.cdx.json", "VERSION"} <= sums.keys()


def build_manifest(bundle, private_path, version, sequence, image_lock,
                   issued_at=None, validity_days=30, source_commit_sha="c" * 40):
    contract = contract_builder().build_contract(
        bundle, version, sequence, "stable", "config/node_roles.json", image_lock,
        "sbom.cdx.json", issued_at or datetime.now(timezone.utc), validity_days,
        {"core_api": 1, "worker_api": 1, "database_schema": 9,
         "database_strategy": "expand", "rollback_safe": True,
         "minimum_version": "0.0.0.1"},
        source_commit_sha,
    )
    signed = sign_release_contract(contract, private_path)
    (bundle / "manifest.json").write_text(
        json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    return signed


def rewrite_manifest(bundle, contract, private_path):
    signed = sign_release_contract(contract, private_path)
    (bundle / "manifest.json").write_text(
        json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")


def qualify_artifact(bundle, public_path, expected_source_sha=None):
    report = module().qualify(ROOT, artifact_root=bundle, public_key=public_path,
                              expected_source_sha=expected_source_sha)
    return report, {check["name"]: check for check in report["checks"]}


def test_repository_passes_static_release_gates():
    report = module().qualify(ROOT)
    assert report["passed"], report
    assert all(check["passed"] for check in report["checks"])


def test_role_isolation_failure_is_reported(tmp_path):
    root = ROOT
    # Issue #36-adjacent: node_roles.json is pretty-printed, so a text replace of a
    # single-line services array no longer matches. Parse + mutate via JSON so the
    # harness is robust to formatting and injects the forbidden service reliably.
    catalog_data = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
    catalog_data["gpu"]["services"].append("postgres")
    catalog = json.dumps(catalog_data, ensure_ascii=False, indent=2)
    for name in ("bootstrap.sh", "deploy/docker-compose.yml", "deploy/proxy.conf",
                 "config/schemas/release-contract.schema.json", "scripts/runtime-contract.py",
                 "scripts/generate-sbom.py",
                 "scripts/apply-deployment.py", "installer/vertep-deployment.service",
                 "installer/vertep-deployment.path",
                 "services/tts_service.py", "services/publisher_service.py", "services/backup_service.py",
                 "docker/tts/Dockerfile", "docker/publisher/Dockerfile", "docker/backup/Dockerfile",
                 "docker/proxy/entrypoint.sh",
                 "monitoring/prometheus.yml", "monitoring/alerts.yml", "monitoring/loki.yml",
                 "monitoring/promtail.yml", "monitoring/grafana/provisioning/datasources/vertep.yml",
                 "monitoring/grafana/provisioning/dashboards/vertep.yml",
                 "monitoring/grafana/dashboards/fleet.json",
                 "scripts/update-agent.py", "scripts/release-layout.py", "installer/update-public.pem"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/node_roles.json").write_text(catalog)
    report = module().qualify(tmp_path)
    assert not report["passed"]
    assert next(item for item in report["checks"] if item["name"] == "role_isolation:gpu")["detail"] == "postgres"


def test_valid_artifact_root_passes_release_gates(tmp_path):
    bundle, private, public, version, sequence, _ = artifact_fixture(tmp_path)
    build_manifest(bundle, private, version, sequence, bundle / "images.json")
    report, gates = qualify_artifact(bundle, public)
    failures = [check for check in report["checks"] if not check["passed"]]
    assert not failures, failures
    for name in ("release_artifact_gates", "manifest_contract_structure",
                 "contract_in_validity_window", "artifact_digests_recomputed",
                 "role_catalog_bound", "sbom_bound", "manifest_signature_verified",
                 "manifest_version_matches_version_file", "manifest_images_match_images_lock",
                 "image_digests_match_manifest", "manifest_sbom_hash_matches_sbom",
                  "manifest_channel_is_stable", "manifest_release_sequence_matches_version",
                  "manifest_source_commit_sha_bound"):
        assert gates[name]["passed"], gates[name]


def test_tampered_artifact_fails_digest_recomputation(tmp_path):
    bundle, private, public, version, sequence, _ = artifact_fixture(tmp_path)
    build_manifest(bundle, private, version, sequence, bundle / "images.json")
    (bundle / "docker-compose.yml").write_text("name: vertep\nhacked\n", encoding="utf-8")
    report, gates = qualify_artifact(bundle, public)
    assert not report["passed"]
    assert not gates["artifact_digests_recomputed"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]


def test_foreign_source_sha_fails_binding(tmp_path):
    """Issue #146: a manifest bound to another commit must not qualify.

    The bundle is otherwise fully valid; only its ``source_commit_sha`` points
    at a different commit, so the qualification must reject it against the
    expected release SHA rather than trusting the field's mere presence.
    """
    bundle, private, public, version, sequence, _ = artifact_fixture(tmp_path)
    build_manifest(bundle, private, version, sequence, bundle / "images.json",
                   source_commit_sha="d" * 40)
    report, gates = qualify_artifact(bundle, public, expected_source_sha="e" * 40)
    assert not report["passed"]
    assert not gates["manifest_source_commit_sha_bound"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]
    # The same bundle qualifies when the expected SHA matches.
    report_ok, gates_ok = qualify_artifact(bundle, public, expected_source_sha="d" * 40)
    assert gates_ok["manifest_source_commit_sha_bound"]["passed"]


def test_signature_from_wrong_key_is_rejected(tmp_path):
    bundle, _, public, version, sequence, _ = artifact_fixture(tmp_path)
    wrong_private, _ = keypair(tmp_path, "wrong")
    build_manifest(bundle, wrong_private, version, sequence, bundle / "images.json")
    report, gates = qualify_artifact(bundle, public)
    assert not report["passed"]
    assert not gates["manifest_signature_verified"]["passed"]
    assert gates["artifact_digests_recomputed"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]


def test_manifest_version_drift_fails(tmp_path):
    bundle, private, public, version, sequence, _ = artifact_fixture(tmp_path)
    build_manifest(bundle, private, version, sequence, bundle / "images.json")
    contract = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    contract["version"] = "9.9.9.9"
    rewrite_manifest(bundle, contract, private)
    report, gates = qualify_artifact(bundle, public)
    assert not report["passed"]
    assert not gates["manifest_version_matches_version_file"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]


def test_images_lock_drift_fails(tmp_path):
    bundle, private, public, version, sequence, images = artifact_fixture(tmp_path)
    drifted = json.loads(json.dumps(images))
    drifted["core"]["digest"] = "sha256:" + "a" * 64
    drifted_lock = tmp_path / "images-drifted.json"
    drifted_lock.write_text(json.dumps(drifted), encoding="utf-8")
    build_manifest(bundle, private, version, sequence, drifted_lock)
    report, gates = qualify_artifact(bundle, public)
    assert not report["passed"]
    assert not gates["manifest_images_match_images_lock"]["passed"]
    assert not gates["image_digests_match_manifest"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]


def test_sbom_hash_drift_fails(tmp_path):
    bundle, private, public, version, sequence, _ = artifact_fixture(tmp_path)
    build_manifest(bundle, private, version, sequence, bundle / "images.json")
    contract = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    contract["sbom"]["sha256"] = "0" * 64
    rewrite_manifest(bundle, contract, private)
    report, gates = qualify_artifact(bundle, public)
    assert not report["passed"]
    assert not gates["manifest_sbom_hash_matches_sbom"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]


def test_expired_contract_fails_validity_window(tmp_path):
    bundle, private, public, version, sequence, _ = artifact_fixture(tmp_path)
    issued = datetime.now(timezone.utc) - timedelta(days=40)
    build_manifest(bundle, private, version, sequence, bundle / "images.json",
                   issued_at=issued, validity_days=10)
    report, gates = qualify_artifact(bundle, public)
    assert not report["passed"]
    assert not gates["contract_in_validity_window"]["passed"]
    assert not gates["release_artifact_gates"]["passed"]
