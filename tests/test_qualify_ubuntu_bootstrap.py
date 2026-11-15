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


# ── Temporary single-release exception for the B3 rollback probe (15.11.2026) ──

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


def test_single_published_release_is_the_dedicated_not_run_case(monkeypatch):
    target = module()
    _releases(monkeypatch, target, ["0.0.3.72"])
    with pytest.raises(target.NoRollbackProbeRelease):
        target.rollback_probe_release("0.0.3.72")


def test_another_published_release_is_still_probed(monkeypatch):
    target = module()
    _releases(monkeypatch, target, ["0.0.3.76", "0.0.3.72"])
    assert target.rollback_probe_release("0.0.3.76") == "0.0.3.72"


def test_configured_probe_release_wins(monkeypatch):
    target = module()
    monkeypatch.setenv("VERTEP_ROLLBACK_PROBE_RELEASE", "0.0.3.60")
    assert target.rollback_probe_release("0.0.3.72") == "0.0.3.60"


def test_the_exception_is_narrow_and_temporary():
    source = (Path(__file__).parents[1] / "scripts" / "qualify_ubuntu_bootstrap.py").read_text(encoding="utf-8")
    # Only the dedicated exception becomes NOT_RUN; any other RuntimeError is still FAIL.
    assert "except NoRollbackProbeRelease as error:" in source
    assert 'report.get("not_run") == ["failure_rollback"]' in source
    assert "TEMPORARY owner-approved exception" in source
