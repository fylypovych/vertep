"""i.0.0.0.11 — Bootstrap Installer & First Run Wizard: smoke, config, and contract tests."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


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
        from core.api.setup import first_run_health
        result = first_run_health()
        assert "ready" in result and "checks" in result

    def test_setup_status_when_not_configured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
        monkeypatch.delenv("NODE_ROLE", raising=False)
        from core.api.setup import first_run_status
        result = first_run_status()
        assert result["configured"] is False
        assert "roles" in result


class TestHealthCheckAccuracy:
    def test_run_checks_covers_all_roles(self):
        from core.health_checks import run_checks
        for role in ("core", "gpu", "text", "voice", "publisher", "backup", "monitoring"):
            with patch.dict(os.environ, {"NODE_ROLE": role}, clear=False):
                checks = run_checks(role)
                assert checks["role"] == role

    def test_health_status_healthy(self):
        from core.health_checks import health_status
        assert health_status({"docker": (True, "ok")}) == "HEALTHY"

    def test_health_status_unhealthy(self):
        from core.health_checks import health_status
        assert health_status({"docker": (False, "err")}) == "UNHEALTHY"


class TestSetupWizardHTML:
    def test_has_all_sections(self):
        html = (ROOT / "web" / "setup.html").read_text(encoding="utf-8")
        for s in ("Роль вузла", "Адміністратор", "Секрети", "Обладнання",
                  "AI Backend", "Health Check", "Installation Manifest", "Готово"):
            assert s in html

    def test_has_admin_fields(self):
        html = (ROOT / "web" / "setup.html").read_text(encoding="utf-8")
        assert 'id="username"' in html and 'id="password"' in html

    def test_has_core_connection_fields(self):
        html = (ROOT / "web" / "setup.html").read_text(encoding="utf-8")
        for f in ("coreUrl", "coreCertificate", "registrationToken"):
            assert f in html

    def test_has_manifest_download(self):
        html = (ROOT / "web" / "setup.html").read_text(encoding="utf-8")
        assert 'id="downloadManifest"' in html

    def test_completion_section(self):
        html = (ROOT / "web" / "setup.html").read_text(encoding="utf-8")
        assert 'id="result"' in html and 'id="certificate"' in html