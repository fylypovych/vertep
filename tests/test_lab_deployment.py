"""Deployment isolation proofs (Issue #107, stage 1).

This laboratory runs on a separate Ubuntu VM with Docker isolation.  These
tests prove, mechanically, that the shipped Compose files and systemd units can
never reach production: no Docker socket, no production volumes, no secret
environment, host-local ports only.  They do not launch containers.

The isolation checks are applied to the **shipped** files as well, so the
configuration that actually runs is proved, not only a fixture
(Issue i.0.0.2.50, L8).
"""

import re
from pathlib import Path

import pytest
from lab.workspace import (FORBIDDEN_ENVIRONMENT, FORBIDDEN_PATHS,
                           check_command, check_compose_mounts, check_environment,
                           check_working_copy, IsolationReport)

REPOSITORY = Path(__file__).parents[1]
SHIPPED_COMPOSE = ("deploy/lab/docker-compose.yml", "deploy/lab/docker-compose.web.yml")
SHIPPED_DOCKERFILES = ("deploy/lab/Dockerfile", "deploy/lab/Dockerfile.sandbox",
                        "deploy/lab/Dockerfile.web")


def test_working_copy_isolation_isolated_developer_copy():
    report = check_working_copy(Path(__file__).parents[1])
    assert report.isolated is True
    assert not report.problems


def test_working_copy_rejects_the_production_tree():
    report = check_working_copy(Path(__file__).parents[1] / "AGENTS.md")
    assert report.isolated is False
    assert report.problems


def test_environment_holds_no_production_credentials():
    report = check_environment({"HOME": "/tmp", "LAB_STATE_DIR": "/var/lib/lab"})
    assert report.isolated is True


def test_environment_with_production_secrets_is_not_isolated():
    report = check_environment({"ADMIN_PASSWORD": "hunter2",
                                "REDIS_URL": "redis://core:6379/0"})
    assert report.isolated is False
    assert any("production variable" in problem for problem in report.problems)


def test_command_cannot_drive_the_production_docker_daemon():
    report = check_command(["docker", "compose", "-f", "../deploy/docker-compose.yml",
                            "exec", "core", "ls", "/data/config"])
    assert report.isolated is False


def test_command_that_is_local_isolated():
    report = check_command(["python", "-m", "pytest", "-q",
                            "tests/test_lab_policy_gate.py"])
    assert report.isolated is True


def test_compose_mounts_stay_outside_production():
    compose = """
services:
  lab-control:
    image: vertep/lab-control:latest
    volumes:
      - lab-data:/var/lib/vertep-lab
      - ../..:/srv/vertep-lab/repo:ro
  lab-sandbox:
    image: vertep/lab-sandbox:latest
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
volumes:
  lab-data:
"""
    report = check_compose_mounts(compose)
    assert report.isolated is True


def test_compose_with_production_mount_is_not_isolated():
    compose = "services:\n  lab: image: lab\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock"
    report = check_compose_mounts(compose)
    assert report.isolated is False
    assert any("docker.sock" in problem for problem in report.problems)


def test_systemd_units_do_not_wrap_production_units():
    deployment = (Path(__file__).parents[1] / "installer" / "vertep-deployment.service").read_text(
        encoding="utf-8")
    lab = (Path(__file__).parents[1] / "installer" / "vertep-lab.service").read_text(
        encoding="utf-8")
    # The two units must stay independent: neither references the other.
    assert "vertep-lab" not in deployment
    assert "vertep-deployment" not in lab
    assert "ExecStart=/usr/bin/docker compose" in lab
    assert "getData" not in lab


# -- L8: the shipped configuration itself is proved isolated ----------------
def _read(relative: str) -> str:
    return (REPOSITORY / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", SHIPPED_COMPOSE)
def test_shipped_compose_mounts_no_production_path(relative):
    report = check_compose_mounts(_read(relative))
    assert report.isolated is True, report.problems
    assert report.problems == []


@pytest.mark.parametrize("relative", SHIPPED_COMPOSE)
def test_shipped_compose_never_reaches_the_production_daemon(relative):
    text = _read(relative)
    assert "docker.sock" not in text
    assert "DOCKER_HOST" not in text
    assert "docker context" not in text


@pytest.mark.parametrize("relative", SHIPPED_COMPOSE)
def test_shipped_compose_keeps_its_own_state_and_volumes(relative):
    text = _read(relative)
    for forbidden in FORBIDDEN_PATHS:
        assert forbidden not in text
    for secret in FORBIDDEN_ENVIRONMENT:
        assert f"{secret}:" not in text
        assert f"{secret}=" not in text


@pytest.mark.parametrize("relative", SHIPPED_COMPOSE)
def test_shipped_compose_publishes_no_fleet_reachable_port(relative):
    published = re.findall(r"-\s*[\"']?([0-9.]+):([^\"'\s]+):(\d+)", _read(relative))
    assert published, f"no published port found in {relative}"
    for host, _, _ in published:
        assert host == "127.0.0.1", f"{relative} publishes a port on {host}"


def test_shipped_lab_compose_declares_only_laboratory_volumes():
    text = _read("deploy/lab/docker-compose.yml")
    volumes = re.findall(r"^\s{2}([a-z0-9-]+):\s*$", text.split("volumes:", 1)[1], re.M)
    assert volumes
    assert all(volume.startswith("lab-") for volume in volumes)


@pytest.mark.parametrize("relative", SHIPPED_DOCKERFILES)
def test_shipped_lab_images_carry_no_production_configuration(relative):
    text = _read(relative)
    for forbidden in FORBIDDEN_PATHS:
        assert forbidden not in text
    for secret in FORBIDDEN_ENVIRONMENT:
        assert f"{secret}=" not in text


def test_shipped_lab_environment_holds_no_production_credentials():
    environment = {}
    for relative in SHIPPED_COMPOSE:
        for line in _read(relative).splitlines():
            match = re.match(r"\s*-\s*([A-Z][A-Z0-9_]*)=", line)
            if match:
                environment[match.group(1)] = ""
    assert environment
    report = check_environment(environment)
    assert report.isolated is True, report.problems


def _service_block(text: str, service: str) -> str:
    collected: list[str] = []
    inside = False
    for line in text.splitlines():
        if re.match(rf"^  {re.escape(service)}:\s*$", line):
            inside = True
            continue
        if inside:
            if re.match(r"^  \S", line):
                break
            collected.append(line)
    assert collected, f"service {service} not found"
    return "\n".join(collected)


def _environment_names(block: str) -> set:
    names: set[str] = set()
    inside = False
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"^\s{4}environment:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        match = re.match(r"^\s*-\s*([A-Z][A-Z0-9_]*)(=|$)", line)
        if match:
            names.add(match.group(1))
            continue
        match = re.match(r"^\s{6}([A-Z][A-Z0-9_]*):", line)
        if match:
            names.add(match.group(1))
            continue
        if stripped:
            break
    return names


def test_the_control_service_carries_the_owner_and_transport_configuration():
    control = _environment_names(_service_block(_read("deploy/lab/docker-compose.yml"),
                                                 "lab-control"))
    for name in ("LAB_OWNER_TOKEN", "LAB_OWNER_IDENTITY", "LAB_GITHUB_TOKEN",
                 "LAB_GITHUB_REPOSITORY", "LAB_GITHUB_API_URL"):
        assert name in control


def test_the_shipped_scheduled_command_touches_only_the_laboratory():
    unit = _read("installer/vertep-lab.service")
    match = re.search(r"^ExecStart=(.+)$", unit, re.M)
    assert match, "the laboratory unit must declare an ExecStart"
    command = match.group(1).strip()
    for forbidden in FORBIDDEN_PATHS:
        assert not re.search(re.escape(forbidden) + r"(?=$|[\s'\":/])", command), \
            f"the scheduled command reaches production path {forbidden}"
    assert "/opt/vertep-lab/docker-compose.yml" in command
    assert command.endswith("up lab-control")
    # The unit runs the laboratory stack only — never the production compose.
    assert "../deploy/docker-compose.yml" not in command
    assert "EnvironmentFile=-/opt/vertep-lab/lab.env" in unit


def test_the_laboratory_command_checker_does_not_confuse_its_own_paths():
    report = check_command(["python", "-m", "lab.cli", "status",
                             "--state-dir", "/var/lib/vertep-lab/state"])
    assert report.isolated is True, report.problems
    production = check_command(["ls", "/opt/vertep/config"])
    assert production.isolated is False


def test_the_untrusted_sandbox_never_receives_owner_or_publication_secrets():
    sandbox = _environment_names(_service_block(_read("deploy/lab/docker-compose.yml"),
                                                "lab-sandbox"))
    for name in ("LAB_OWNER_TOKEN", "LAB_GITHUB_TOKEN", "LAB_ROLE_EXECUTOR_TOKEN"):
        assert name not in sandbox
