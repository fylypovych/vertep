"""#523 / #491 L2, L4, L8: administrator-only LAB API, one settings store, TLS-only panel."""

import json
import re
import stat
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from lab import settings as lab_settings
from lab.api import PUBLIC_PATHS, create_app
from lab.cli import main
from tests.lab_admin import admin_assertion, vertep_identity  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
ENVS = ["LAB_GITHUB_REPOSITORY", "LAB_RESOURCE_URL", "LAB_VERTEP_URL", "LAB_IDLE_SHARING_ENABLED",
        "LAB_GITHUB_TOKEN", "LAB_RESOURCE_TOKEN", "LAB_RESOURCE_CA_FILE"]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path, vertep_identity):
    # monkeypatch restores every variable the settings overlay touches.
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAB_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("LAB_EVIDENCE_ROOT", str(tmp_path))
    yield
    lab_settings._overlaid.clear()


@pytest.fixture
def app(tmp_path):
    return create_app(state_dir=tmp_path / "state", workspace_root=tmp_path / "repo")


def _routes(app):
    for route in app.routes:
        if route.path.startswith("/api/lab/") and route.path not in PUBLIC_PATHS:
            for method in sorted(route.methods - {"HEAD"}):
                yield method, route.path.replace("{kind}", "i").replace("{grant_id}", "g-1")


def test_every_lab_api_route_requires_an_administrator(app):
    client = TestClient(app)
    routes = list(_routes(app))
    assert len(routes) >= 25 and ("PUT", "/api/lab/settings") in routes
    for method, path in routes:
        assert client.request(method, path, json={}).status_code == 401, (method, path)
        assert client.request(method, path, json={},
                              headers={"X-Lab-Owner-Token": "wrong"}).status_code == 401, (method, path)
    for path in ("/api/lab/healthz", "/api/lab/readyz"):
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/api/lab/status", "/api/lab/audit", "/api/lab/audit/records", "/api/lab/grants",
                                  "/api/lab/issues", "/api/lab/evidence", "/api/lab/rights-requests",
                                  "/api/lab/transfers", "/api/lab/gpu", "/api/lab/notifications",
                                  "/api/lab/reports", "/api/lab/settings", "/api/lab/issues/i"])
def test_reads_succeed_with_a_vertep_identity(app, path):
    client = TestClient(app)
    assert client.get(path, headers={"X-Vertep-Identity": admin_assertion()}).status_code == 200
    assert client.get(path, headers={"X-Lab-Owner-Token": "owner-secret"}).status_code == 200


def test_login_entry_is_public_and_points_to_vertep(app, monkeypatch):
    client = TestClient(app, follow_redirects=False)
    assert client.get("/api/lab/login", params={"return_to": "https://lab/"}).status_code == 409
    monkeypatch.setenv("LAB_VERTEP_URL", "https://vertep.example:8443/")
    response = client.get("/api/lab/login", params={"return_to": "https://lab.example:8444/"})
    assert response.status_code == 302
    assert response.headers["location"] == "https://vertep.example:8443/lab"


# -- L4: one settings store for API and CLI --------------------------------------------

def _pem() -> str:
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vertep-ca")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_settings_api_cli_parity_secrets_write_only_and_audited_by_name(app, tmp_path, monkeypatch, capsys):
    client = TestClient(app)
    admin = {"X-Vertep-Identity": admin_assertion()}
    payload = {"github_repository": "acme/lab", "resource_url": "https://core.example:8443/lab-lease/v1/",
               "vertep_url": "https://core.example:8443", "idle_sharing_enabled": True,
               "github_token": "ghp-top-secret", "resource_token": "resource-top-secret", "resource_ca_pem": _pem()}
    assert client.put("/api/lab/settings", json={"settings": payload}, headers={}).status_code == 401
    saved = client.put("/api/lab/settings", json={"settings": payload}, headers=admin)
    assert saved.status_code == 200, saved.text
    shown = client.get("/api/lab/settings", headers=admin).json()
    assert shown["github_repository"] == "acme/lab" and shown["idle_sharing_enabled"] is True
    assert shown["resource_url"] == "https://core.example:8443/lab-lease/v1"
    assert shown["github_token_set"] is True and shown["resource_token_set"] is True
    text = json.dumps(shown)
    assert "top-secret" not in text and "github_token" not in shown and "resource_token" not in shown
    state = tmp_path / "state"
    assert stat.S_IMODE((state / "settings.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "resource-ca.pem").stat().st_mode) == 0o600
    trail = json.dumps(app.state.context["gate"].trail.records())
    assert "lab_settings:update" in trail and "top-secret" not in trail and "BEGIN CERTIFICATE" not in trail
    assert client.put("/api/lab/settings", json={"settings": {"unknown": 1}}, headers=admin).status_code == 422
    assert client.put("/api/lab/settings", json={"settings": {"resource_url": "ftp://x"}},
                      headers=admin).status_code == 422

    # The CLI reads the very same store, and its owner write lands in the same API view.
    base = ["--state-dir", str(state), "--workspace", str(tmp_path / "repo")]
    assert main([*base, "settings", "show"]) == 0
    assert json.loads(capsys.readouterr().out) == shown
    assert main([*base, "settings", "set", "github_repository=acme/other"]) == 1  # no owner proof
    capsys.readouterr()
    secret_file = tmp_path / "token"
    secret_file.write_text("ghp-from-file\n")
    assert main([*base, "settings", "set", "github_repository=acme/other", f"github_token=@{secret_file}",
                 "idle_sharing_enabled=false", "--owner-token", "owner-secret"]) == 0
    cli_view = json.loads(capsys.readouterr().out)
    assert "ghp-from-file" not in json.dumps(cli_view)
    api_view = client.get("/api/lab/settings", headers=admin).json()
    assert api_view == cli_view and api_view["github_repository"] == "acme/other"
    assert api_view["idle_sharing_enabled"] is False
    stored = json.loads((state / "settings.json").read_text())
    assert stored["github_token"] == "ghp-from-file"


def test_stored_settings_override_the_environment_with_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_GITHUB_REPOSITORY", "env/repo")
    monkeypatch.setenv("LAB_RESOURCE_URL", "https://env.example")
    app = create_app(state_dir=tmp_path / "state", workspace_root=tmp_path / "repo")
    client = TestClient(app)
    admin = {"X-Lab-Owner-Token": "owner-secret"}
    view = client.get("/api/lab/settings", headers=admin).json()
    assert view["github_repository"] == "env/repo" and view["sources"]["github_repository"] == "env"
    client.put("/api/lab/settings", json={"settings": {"github_repository": "stored/repo"}}, headers=admin)
    import os
    assert os.environ["LAB_GITHUB_REPOSITORY"] == "stored/repo"  # what governance reads
    assert os.environ["LAB_RESOURCE_URL"] == "https://env.example"  # untouched env fallback
    # A restart applies the store again; clearing the value falls back to lab.env.
    create_app(state_dir=tmp_path / "state", workspace_root=tmp_path / "repo")
    assert os.environ["LAB_GITHUB_REPOSITORY"] == "stored/repo"
    client.put("/api/lab/settings", json={"settings": {"github_repository": ""}}, headers=admin)
    assert os.environ["LAB_GITHUB_REPOSITORY"] == "env/repo"
    assert client.get("/api/lab/settings", headers=admin).json()["sources"]["github_repository"] == "env"


# -- L2/L8: the panel is published only over TLS ---------------------------------------

def test_panel_publishes_only_a_tls_listener():
    compose = yaml.safe_load((ROOT / "deploy/lab/docker-compose.web.yml").read_text())
    published = {name: service.get("ports", []) for name, service in compose["services"].items()}
    assert published["lab-control"] == [] and published["lab-executor"] == []
    assert published["lab-web"] == ["${LAB_WEB_BIND:-0.0.0.0}:${LAB_WEB_PORT:-8444}:8443"]
    assert any(str(volume).endswith(":/run/lab-tls:ro") for volume in compose["services"]["lab-web"]["volumes"])
    for name, service in compose["services"].items():
        assert "build" not in service, name  # the runtime compose runs signed images only (L5)
        mounts = " ".join(map(str, service.get("volumes", [])))
        assert "docker.sock" not in mounts and "/opt/vertep" not in mounts
    nginx = (ROOT / "deploy/lab/web/nginx.conf").read_text()
    listens = re.findall(r"^\s*listen\s+([^;]+);", nginx, re.M)
    # One HTTPS listener; the plain one is the in-container probe on loopback only.
    assert listens == ["127.0.0.1:8080", "8443 ssl"]
    assert "ssl_certificate /run/lab-tls/lab.crt;" in nginx and "ssl_certificate_key /run/lab-tls/lab.key;" in nginx
