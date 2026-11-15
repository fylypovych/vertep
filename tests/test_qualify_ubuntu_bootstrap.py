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


# ── B3 rollback probe: release selection (temporary exception removed in 0.0.3.78) ──

import io
import json

import pytest


def _releases(monkeypatch, target, tags):
    payload = json.dumps([{"tag_name": tag, "draft": False, "prerelease": False} for tag in tags]).encode()

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.delenv("VERTEP_ROLLBACK_PROBE_RELEASE", raising=False)
    monkeypatch.setattr(target.urllib.request, "urlopen", lambda *a, **k: Answer(payload))


def test_a_single_published_release_fails_the_probe_again(monkeypatch):
    target = module()
    _releases(monkeypatch, target, ["0.0.3.77"])
    with pytest.raises(RuntimeError, match="no other published release"):
        target.rollback_probe_release("0.0.3.77")


def test_another_published_release_is_probed(monkeypatch):
    target = module()
    _releases(monkeypatch, target, ["0.0.3.77", "0.0.3.72"])
    assert target.rollback_probe_release("0.0.3.77") == "0.0.3.72"


def test_configured_probe_release_wins(monkeypatch):
    target = module()
    monkeypatch.setenv("VERTEP_ROLLBACK_PROBE_RELEASE", "0.0.3.72")
    assert target.rollback_probe_release("0.0.3.77") == "0.0.3.72"


def test_the_temporary_not_run_exception_is_gone():
    source = (Path(__file__).parents[1] / "scripts" / "qualify_ubuntu_bootstrap.py").read_text(encoding="utf-8")
    assert "NoRollbackProbeRelease" not in source and "not_run" not in source
    assert 'return 0 if report["result"] == "PASS" else 1' in source
