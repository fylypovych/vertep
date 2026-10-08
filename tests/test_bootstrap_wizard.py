"""i.0.0.0.11 — Bootstrap Installer & First Run Wizard: smoke, config, and contract tests."""
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).parents[1]


# ── Bootstrap script structure ───────────────────────────────────────

class TestBootstrapPreflight:
    def test_bootstrap_requires_root(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "EUID" in bs or "id -u" in bs

    def test_bootstrap_requires_ubuntu_2404(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "ubuntu" in bs and "24.04" in bs

    def test_bootstrap_checks_ram(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "MemTotal" in bs and "MIN_RAM_MB" in bs

    def test_bootstrap_checks_disk(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "MIN_DISK_MB" in bs and "df" in bs

    def test_bootstrap_checks_architecture(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "dpkg --print-architecture" in bs

    def test_bootstrap_checks_dns(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "getent ahosts" in bs

    def test_bootstrap_checks_ntp(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "NTPSynchronized" in bs

    def test_bootstrap_checks_port(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "8443" in bs

    def test_bootstrap_checks_filesystem(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "findmnt" in bs and "ext4" in bs


class TestBootstrapManifest:
    def test_manifest_schema_check(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert '.schema == 2' in bs and '.product == "vertep"' in bs

    def test_manifest_sbom_format(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "CycloneDX" in bs

    def test_manifest_signature_verification(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "openssl dgst -sha256 -verify" in bs
        assert "BOOTSTRAP_PUBLIC_KEY" in bs

    def test_bootstrap_public_keys_identical_and_match_installer_key(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        blocks = re.findall(r"<<'BOOTSTRAP_PUBLIC_KEY'\n(.*?)\nBOOTSTRAP_PUBLIC_KEY", bs, re.DOTALL)
        assert len(blocks) == 2
        assert blocks[0] == blocks[1]
        expected = (ROOT / "installer/update-public.pem").read_text(encoding="utf-8")
        assert blocks[0].strip() == expected.strip()

    def test_manifest_sha256_checksums(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "sha256sum -c -" in bs

    def test_manifest_roles_catalog(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "catalog_sha256" in bs and ".roles.profiles" in bs


class TestBootstrapSecrets:
    def test_postgres_password_persistence(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert 'existing_secret "$INSTALL_ROOT/config/postgres.password" POSTGRES_PASSWORD' in bs

    def test_redis_password_persistence(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert 'existing_secret "$INSTALL_ROOT/config/redis.password" REDIS_PASSWORD' in bs

    def test_secret_store_passphrase(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "secret-store.passphrase" in bs

    def test_tls_certificate_persistence(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "existing TLS certificate/key pair is incomplete" in bs

    def test_deployment_plan_not_overwritten(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert '[[ ! -f "$INSTALL_ROOT/config/deployment-plan.json" ]]' in bs

    def test_secrets_not_overwritten(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert '[[ ! -f "$INSTALL_ROOT/config/secrets.enc.json"' in bs


class TestBootstrapGPU:
    def test_nvidia_container_toolkit(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "nvidia-container-toolkit" in bs and "nvidia-ctk runtime configure" in bs

    def test_amd_rocm_runtime(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "rocm-hip-runtime" in bs and "rocminfo" in bs


class TestBootstrapManagedEnv:
    def test_managed_keys_merge(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "existing_env_tmp=$(mktemp)" in bs
        assert 'managed_keys = {line.split("=", 1)[0]' in bs

    def test_preserves_existing_node_role(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "existing_node_role=$(env_value NODE_ROLE)" in bs

    def test_preserves_existing_web_domain(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "existing_web_domain=$(env_value WEB_DOMAIN)" in bs


class TestBootstrapHealthLoop:
    def test_health_check_function_exists(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert 'required_services_healthy(){' in bs

    def test_migrate_excluded(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert '[[ $service == migrate ]] && continue' in bs

    def test_health_report_interval(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert 'next_health_report=$((SECONDS+30))' in bs


class TestInstallScript:
    def test_enables_core_services(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        assert "systemctl enable --now vertep-core.service" in install
        assert "systemctl enable --now vertep-worker.service" in install

    def test_installs_cli(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        assert 'ln -sfn "$ROOT_DIR/scripts/vertep" /usr/local/bin/vertep' in install

    def test_ubuntu_only(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        assert "ubuntu:24.04" in install

    def test_ufw_firewall(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        assert "ufw default deny incoming" in install

    def test_ssh_hardening(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        assert "PasswordAuthentication no" in install
        assert "sshd -t" in install

    def test_gpu_driver_wait(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        assert "if ! nvidia-smi >/dev/null 2>&1" in install
class TestManifestContract:
    def test_manifest_has_all_roles(self):
        manifest = json.loads((ROOT / "installer" / "manifest.json").read_text(encoding="utf-8"))
        for role in ("core", "gpu", "text", "voice", "publisher", "backup", "monitoring"):
            assert role in manifest["roles"]

    def test_core_packages(self):
        manifest = json.loads((ROOT / "installer" / "manifest.json").read_text(encoding="utf-8"))
        for pkg in ("docker.io", "python3", "ffmpeg", "curl", "ufw"):
            assert pkg in manifest["roles"]["core"]["packages"]

    def test_core_services(self):
        manifest = json.loads((ROOT / "installer" / "manifest.json").read_text(encoding="utf-8"))
        for svc in ("docker", "vertep-core", "vertep-update.path"):
            assert svc in manifest["roles"]["core"]["services"]

    def test_profiles_define_includes(self):
        manifest = json.loads((ROOT / "installer" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["profiles"]["core-worker"]["includes"] == ["core", "gpu"]


class TestPreflightScript:
    def test_checks_disk(self):
        pf = (ROOT / "installer" / "preflight.sh").read_text(encoding="utf-8")
        assert "Disk available" in pf

    def test_checks_commands(self):
        pf = (ROOT / "installer" / "preflight.sh").read_text(encoding="utf-8")
        for cmd in ("git", "python3", "ffmpeg", "docker"):
            assert cmd in pf

    def test_checks_ssh_key(self):
        pf = (ROOT / "installer" / "preflight.sh").read_text(encoding="utf-8")
        assert "SSH key" in pf

    def test_checks_port(self):
        pf = (ROOT / "installer" / "preflight.sh").read_text(encoding="utf-8")
        assert "8080" in pf


class TestSetupAPI:
    def test_setup_endpoints_exist(self):
        from core.api.setup import router
        paths = {route.path for route in router.routes}
        assert "/api/setup" in paths
        assert "/api/setup/health" in paths
        assert "/api/setup/complete" in paths

    def test_health_check_structure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        monkeypatch.setenv("NODE_ROLE", "core")
        import core.health_checks as hc
        monkeypatch.setattr(hc, "run_checks",
                            lambda role: {"role": role, "checked_at": "x",
                                          "docker": (True, "ok")})
        monkeypatch.setattr(hc, "health_status", lambda c: "HEALTHY")
        from core.api.setup import first_run_health
        result = first_run_health()
        assert "ready" in result and "checks" in result
        assert result["checks"]["docker"] == "OK"
        assert result["status"] == "HEALTHY"
        assert result["role"] == "core"

    def test_setup_status_when_not_configured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        monkeypatch.delenv("NODE_ROLE", raising=False)
        from core.api.setup import first_run_status
        result = first_run_status()
        assert result["configured"] is False
        assert "roles" in result

    def test_server_reads_ai_backend_field(self):
        """i.0.0.0.98: the complete endpoint must read ``ai_backend``."""
        source = (ROOT / "core" / "api" / "setup.py").read_text(encoding="utf-8")
        assert 'payload.get("ai_backend", "skip")' in source

    def test_health_uses_shared_health_checks_for_redis_ollama_docker(self, monkeypatch):
        """i.0.0.0.98: setup health must actually probe Redis/Ollama/Docker,
        not infer them from config or a static hardware string."""
        monkeypatch.setenv("NODE_ROLE", "core")
        monkeypatch.setenv("REDIS_URL", "redis://probe:6379")
        monkeypatch.setenv("OLLAMA_URL", "http://ollama-probe:11434")
        calls = []
        import core.health_checks as hc

        def fake_run_checks(role):
            calls.append(role)
            return {
                "role": role,
                "checked_at": "2026-01-01T00:00:00+00:00",
                "docker": (True, "ok"),
                "postgres": (True, "ok"),
                "redis": (True, "ok"),
                "core_api": (True, "local"),
                "ollama": (True, "ok"),
                "monitoring": (None, "not-applicable"),
            }

        monkeypatch.setattr(hc, "run_checks", fake_run_checks)
        monkeypatch.setattr(hc, "health_status", lambda checks: "HEALTHY")
        from core.api.setup import first_run_health
        result = first_run_health()
        assert calls == ["core"]
        assert result["checks"]["redis"] == "OK"
        assert result["checks"]["ollama"] == "OK"
        assert result["checks"]["docker"] == "OK"
        assert result["checks"]["monitoring"] == "OPTIONAL"
        assert result["status"] == "HEALTHY"
        assert result["role"] == "core"

    def test_health_blocks_on_real_failure(self, monkeypatch):
        """i.0.0.0.98: a genuine OFFLINE/UNAVAILABLE check blocks the ready gate."""
        monkeypatch.setenv("NODE_ROLE", "core")
        import core.health_checks as hc
        monkeypatch.setattr(hc, "run_checks", lambda role: {
            "role": role, "checked_at": "x",
            "docker": (True, "ok"),
            "postgres": (False, "connection refused"),
        })
        monkeypatch.setattr(hc, "health_status", lambda c: "UNHEALTHY")
        from core.api.setup import first_run_health
        result = first_run_health()
        assert result["ready"] is False
        assert result["checks"]["postgres"].startswith("OFFLINE")

    def test_health_is_role_aware(self, monkeypatch):
        """i.0.0.0.98: a GPU node must not be asked to probe PostgreSQL/Redis."""
        monkeypatch.setenv("NODE_ROLE", "gpu")
        seen = []
        import core.health_checks as hc
        monkeypatch.setattr(hc, "run_checks",
                            lambda role: seen.append(role) or {
                                "role": role, "checked_at": "x",
                                "docker": (True, "ok"),
                                "gpu": (None, "not-applicable"),
                                "cuda": (None, "not-applicable"),
                                "comfyui": (None, "not-applicable"),
                            })
        monkeypatch.setattr(hc, "health_status", lambda c: "HEALTHY")
        from core.api.setup import first_run_health
        first_run_health()
        assert seen == ["gpu"]


class TestSetupBackendFieldAlignment:
    """i.0.0.0.98: the Angular wizard must send ``ai_backend`` so the server
    actually persists the backend the user selected instead of defaulting to
    ``skip``."""

    def test_wizard_sends_ai_backend_not_backend(self):
        """i.0.0.0.98: the Angular payload key for the AI backend is
        ``ai_backend`` (the server-side field name), not a bare ``backend``
        key that the server would otherwise ignore."""
        source = (ROOT / "web-v2" / "src" / "app" / "setup" / "setup.component.ts").read_text(
            encoding="utf-8")
        complete_call = re.search(
            r"const p: Record<string, unknown> = \{[^}]*\}", source, re.DOTALL)
        assert complete_call is not None
        payload = complete_call.group(0)
        assert "ai_backend:" in payload
        # The backend *value* source is backend_selected; the bare key must
        # not be used, only backend_model / backend_api_key.
        assert re.search(r"\bbackend:\s", payload) is None
        assert "backend_url:" in payload

    def test_server_reads_ai_backend(self):
        source = (ROOT / "core" / "api" / "setup.py").read_text(encoding="utf-8")
        assert 'payload.get("ai_backend", "skip")' in source

    def test_server_reads_ai_backend_field(self):
        """i.0.0.0.98: the complete endpoint reads ``ai_backend``."""
        source = (ROOT / "core" / "api" / "setup.py").read_text(encoding="utf-8")
        assert 'payload.get("ai_backend", "skip")' in source

    def test_external_backend_requires_url(self, monkeypatch):
        """i.0.0.0.98: the ``external`` backend must require ``backend_url``."""
        import asyncio
        import core.app as core_app
        import core.api.setup as setup_mod

        async def fake_validate(backend, url, model, api_key):
            if backend == "external" and not url:
                raise ValueError("External AI backend requires a URL")

        def fake_complete(*args, **kwargs):
            return {"configured": True}

        class Request:
            base_url = "https://vertep.example/"

            async def json(self):
                return {"node_role": "core", "installation_name": "Vertep",
                        "username": "admin", "password": "a-secure-password",
                        "password_confirmation": "a-secure-password",
                        "ai_backend": "external", "backend_model": "my-model"}

        monkeypatch.setattr(setup_mod, "_validate_ai_backend", fake_validate)
        monkeypatch.setattr(setup_mod, "complete_setup", fake_complete)
        monkeypatch.setattr(setup_mod, "create_registration_token",
                            lambda *a, **k: {"token": "one-time"})
        with pytest.raises(HTTPException, match="422"):
            asyncio.run(core_app.first_run_complete(Request()))

    def test_backend_selection_is_not_silently_dropped(self, monkeypatch):
        """i.0.0.0.98: the wizard must send ``ai_backend`` and the server must
        actually persist the backend the user selected instead of falling
        back to ``skip``."""
        import asyncio
        import core.app as core_app
        import core.api.setup as setup_mod

        captured = {}

        async def fake_validate(backend, url, model, api_key):
            captured["backend"] = backend

        def fake_complete(*args, **kwargs):
            captured["complete_backend"] = args[4]
            return {"configured": True}

        class Request:
            base_url = "https://vertep.example/"

            async def json(self):
                return {"node_role": "core", "installation_name": "Vertep",
                        "username": "admin", "password": "a-secure-password",
                        "password_confirmation": "a-secure-password",
                        "ai_backend": "ollama"}

        monkeypatch.setattr(setup_mod, "_validate_ai_backend", fake_validate)
        monkeypatch.setattr(setup_mod, "complete_setup", fake_complete)
        monkeypatch.setattr(setup_mod, "create_registration_token",
                            lambda *a, **k: {"token": "one-time"})
        asyncio.run(core_app.first_run_complete(Request()))
        assert captured["backend"] == "ollama"
        assert captured["complete_backend"] == "ollama"


class TestSetupWizardHTML:
    def test_has_all_sections(self):
        html = (ROOT / "web-v2" / "src" / "app" / "setup" / "setup.component.html").read_text(encoding="utf-8")
        for s in ("Крок 1: Роль вузла", "Крок 2: Назва та адміністратор",
                  "Крок 4: AI Backend", "Крок 5: Перевірка системи", "Готово"):
            assert s in html

    def test_has_admin_fields(self):
        html = (ROOT / "web-v2" / "src" / "app" / "setup" / "setup.component.html").read_text(encoding="utf-8")
        assert 'data-testid="setup-username"' in html and 'data-testid="setup-password"' in html

    def test_has_core_connection_fields(self):
        html = (ROOT / "web-v2" / "src" / "app" / "setup" / "setup.component.html").read_text(encoding="utf-8")
        for f in ("setup-core-url", "setup-core-cert", "setup-reg-token"):
            assert f"data-testid=\"{f}\"" in html

    def test_has_manifest_download(self):
        html = (ROOT / "web-v2" / "src" / "app" / "setup" / "setup.component.html").read_text(encoding="utf-8")
        assert "Installation ID" in html or "installation_id" in html

    def test_completion_section(self):
        html = (ROOT / "web-v2" / "src" / "app" / "setup" / "setup.component.html").read_text(encoding="utf-8")
        assert 'data-testid="setup-complete"' in html and "Core URL" in html


class TestBootstrapDockerCompose:
    def test_docker_version_check(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "docker_version" in bs and "too old" in bs

    def test_compose_version_check(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "compose_version" in bs and "too old" in bs

    def test_docker_compose_post_install_verification(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "docker compose version" in bs

    def test_docker_min_version(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "24.0" in bs

    def test_compose_min_version(self):
        bs = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        assert "v2" in bs