import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.release_contract import sign_release_contract, validate_release_contract


def contract_builder():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts" / "runtime-contract.py"
    spec = importlib.util.spec_from_file_location("runtime_contract", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def release_fixture(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    roles = {
        "core": {"services": ["core", "postgres"], "capabilities": ["scheduling"],
                 "modules": ["core", "postgresql"]},
        "gpu": {"services": ["worker", "comfyui"], "capabilities": ["image_generation"],
                "modules": ["worker", "comfyui"]},
    }
    (bundle / "node_roles.json").write_text(json.dumps(roles), encoding="utf-8")
    (bundle / "docker-compose.yml").write_text("name: vertep\n", encoding="utf-8")
    (bundle / "sbom.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}),
                                           encoding="utf-8")
    images = {service: {"reference": f"registry.vertep.ai/vertep/{service}",
                        "digest": "sha256:" + character * 64,
                        "platforms": ["linux/amd64"]}
              for service, character in zip(("core", "postgres", "worker", "comfyui"), "abcd")}
    image_lock = tmp_path / "images.json"
    image_lock.write_text(json.dumps(images), encoding="utf-8")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path, public_path = tmp_path / "private.pem", tmp_path / "public.pem"
    private_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return bundle, image_lock, private_path, public_path


def test_release_contract_binds_roles_images_files_and_sbom(tmp_path):
    bundle, image_lock, private_key, public_key = release_fixture(tmp_path)
    issued_at = datetime.now(timezone.utc)
    contract = contract_builder().build_contract(
        bundle, "0.0.0.12", 12, "stable", "node_roles.json", image_lock,
        "sbom.cdx.json", issued_at, 30,
        {"core_api": 1, "worker_api": 1, "database_schema": 7,
         "database_strategy": "expand", "rollback_safe": True,
         "minimum_version": "0.0.0.5"},
    )
    signed = sign_release_contract(contract, private_key)
    assert validate_release_contract(signed, bundle, public_key)["release_sequence"] == 12
    assert signed["roles"]["profiles"]["gpu"]["services"] == ["worker", "comfyui"]

    altered_profile = json.loads(json.dumps(signed))
    altered_profile["roles"]["profiles"]["gpu"]["services"] = ["worker"]
    altered_profile = sign_release_contract(altered_profile, private_key)
    with pytest.raises(RuntimeError, match="does not match the role catalog"):
        validate_release_contract(altered_profile, bundle, public_key)

    (bundle / "docker-compose.yml").write_text("name: hacked\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        validate_release_contract(signed, bundle, public_key)


def test_release_contract_rejects_unpinned_role_service(tmp_path):
    bundle, image_lock, private_key, _ = release_fixture(tmp_path)
    images = json.loads(image_lock.read_text(encoding="utf-8"))
    images.pop("comfyui")
    image_lock.write_text(json.dumps(images), encoding="utf-8")
    with pytest.raises(ValueError, match="missing role services"):
        contract_builder().build_contract(
            bundle, "0.0.0.12", 12, "stable", "node_roles.json", image_lock,
            "sbom.cdx.json", datetime.now(timezone.utc), 30,
            {"core_api": 1, "worker_api": 1, "database_schema": 7,
             "database_strategy": "none", "rollback_safe": True,
             "minimum_version": "0.0.0.5"},
        )
