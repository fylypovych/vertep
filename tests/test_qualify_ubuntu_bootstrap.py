import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).parents[1] / "scripts" / "qualify_ubuntu_bootstrap.py"
    spec = importlib.util.spec_from_file_location("qualify_ubuntu_bootstrap", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_containers_are_ready_only_when_every_serving_container_runs_healthy():
    target = module()
    healthy = [{"service": "core", "state": "running", "health": "healthy"},
               {"service": "proxy", "state": "running", "health": ""},
               {"service": "migrate", "state": "exited", "health": ""}]
    assert target.containers_ready(healthy)
    assert not target.containers_ready([*healthy, {"service": "backup-service",
                                                   "state": "restarting", "health": "unhealthy"}])
    assert not target.containers_ready([{"service": "migrate", "state": "exited", "health": ""}])


def test_credentials_are_compared_without_the_regenerated_setup_token(tmp_path, monkeypatch):
    target = module()
    monkeypatch.setattr(target, "INSTALL_ROOT", tmp_path)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=a\nJWT_SECRET=b\nSETUP_TOKEN=c\nVERTEP_VERSION=1\n",
                                   encoding="utf-8")
    fingerprints = target.secret_fingerprints()
    assert set(fingerprints) == {"POSTGRES_PASSWORD", "JWT_SECRET"}
    assert "a" not in fingerprints.values()
