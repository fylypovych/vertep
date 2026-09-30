"""Issue #80: the outbound-only connectivity model must be verified mechanically.

Non-Core roles reach the fleet exclusively by dialing out to Core.  A published
host port on a worker-role service silently breaks that model behind NAT/VPN, so
the published Compose surface is checked instead of merely documented.
"""
import json
from pathlib import Path

from core import outbound_only


ROLES = {
    "core": {"services": ["proxy", "core", "postgres"], "capabilities": ["scheduling"]},
    "gpu": {"services": ["worker", "comfyui", "update-agent"], "capabilities": ["image_generation"]},
    "text": {"services": ["worker", "ollama", "update-agent"], "capabilities": ["text_generation"]},
    "voice": {"services": ["worker", "tts", "update-agent"], "capabilities": ["speech_synthesis"]},
    "publisher": {"services": ["worker", "publisher-worker", "update-agent"], "capabilities": ["publishing"]},
    "backup": {"services": ["worker", "backup-service", "update-agent"], "capabilities": ["backup"]},
    "monitoring": {"services": ["worker", "monitoring", "grafana", "update-agent"],
                   "capabilities": ["metrics"]},
}


def _compose(tmp_path: Path, body: str) -> Path:
    (tmp_path / "docker-compose.yml").write_text(body, encoding="utf-8")
    (tmp_path / "node_roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
    return tmp_path


def test_non_core_role_services_come_from_the_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_ROLES_FILE", str(tmp_path / "node_roles.json"))
    (tmp_path / "node_roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
    services = outbound_only.non_core_role_services()
    for expected in ("comfyui", "ollama", "tts", "publisher-worker", "backup-service", "grafana"):
        assert expected in services
    assert "proxy" not in services
    assert "postgres" not in services


def test_repository_compose_files_publish_no_inbound_worker_ports():
    """The shipped Compose files must keep every non-Core role outbound-only."""
    root = Path(__file__).parents[1]
    report = outbound_only.check_outbound_only(root)
    assert report["checked_files"], "no Compose files were checked"
    assert report["violations"] == [], report["violations"]
    assert report["outbound_only"] is True


def test_worker_role_port_publication_is_detected(tmp_path, monkeypatch):
    path = _compose(tmp_path, "services:\n  worker:\n    ports: [\"8188:8188\"]\n")
    monkeypatch.setenv("NODE_ROLES_FILE", str(path / "node_roles.json"))
    violations = outbound_only.inbound_port_violations(path / "docker-compose.yml")
    assert len(violations) == 1
    assert violations[0]["service"] == "worker"
    assert violations[0]["port"] == "8188:8188"


def test_loopback_publication_is_allowed(tmp_path, monkeypatch):
    """PostgreSQL bound to loopback is host-local, not fleet-reachable."""
    path = _compose(tmp_path,
                    "services:\n  postgres:\n    ports: [\"127.0.0.1:5432:5432\"]\n"
                    "  core:\n    ports: [\"8443:8443\"]\n")
    monkeypatch.setenv("NODE_ROLES_FILE", str(path / "node_roles.json"))
    assert outbound_only.inbound_port_violations(path / "docker-compose.yml") == []


def test_core_role_publication_is_allowed(tmp_path, monkeypatch):
    path = _compose(tmp_path,
                    "services:\n  proxy:\n    ports: [\"8443:8443\"]\n"
                    "  core:\n    ports: [\"8080:8080\"]\n")
    monkeypatch.setenv("NODE_ROLES_FILE", str(path / "node_roles.json"))
    assert outbound_only.inbound_port_violations(path / "docker-compose.yml") == []


def test_check_reports_every_violation(tmp_path, monkeypatch):
    path = _compose(tmp_path,
                    "services:\n  worker:\n    ports: [\"8188:8188\"]\n"
                    "  comfyui:\n    ports: [\"8189:8189\"]\n"
                    "  core:\n    ports: [\"8080:8080\"]\n")
    monkeypatch.setenv("NODE_ROLES_FILE", str(path / "node_roles.json"))
    report = outbound_only.check_outbound_only(path)
    assert report["outbound_only"] is False
    assert {item["service"] for item in report["violations"]} == {"worker", "comfyui"}


def test_check_tolerates_a_missing_compose_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_ROLES_FILE", str(tmp_path / "node_roles.json"))
    (tmp_path / "node_roles.json").write_text(json.dumps(ROLES), encoding="utf-8")
    report = outbound_only.check_outbound_only(tmp_path)
    assert report["outbound_only"] is True
    assert report["violations"] == []
