import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).parents[1] / "scripts" / "qualify-release.py"
    spec = importlib.util.spec_from_file_location("qualify_release", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def test_repository_passes_static_release_gates():
    report = module().qualify(Path(__file__).parents[1])
    assert report["passed"], report
    assert all(check["passed"] for check in report["checks"])


def test_role_isolation_failure_is_reported(tmp_path):
    root = Path(__file__).parents[1]
    catalog = (root / "config/node_roles.json").read_text().replace(
        '"services": ["worker", "comfyui", "update-agent"]',
        '"services": ["worker", "comfyui", "postgres", "update-agent"]')
    for name in ("bootstrap.sh", "deploy/docker-compose.yml", "deploy/proxy.conf",
                 "config/schemas/release-contract.schema.json", "scripts/runtime-contract.py",
                 "scripts/generate-sbom.py",
                 "services/tts_service.py", "services/publisher_service.py", "services/backup_service.py",
                 "docker/tts/Dockerfile", "docker/publisher/Dockerfile", "docker/backup/Dockerfile",
                 "scripts/update-agent.py", "scripts/release-layout.py", "installer/update-public.pem"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/node_roles.json").write_text(catalog)
    report = module().qualify(tmp_path)
    assert not report["passed"]
    assert next(item for item in report["checks"] if item["name"] == "role_isolation:gpu")["detail"] == "postgres"
