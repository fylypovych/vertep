"""Deployment isolation proofs (Issue #107, stage 1).

This laboratory runs on a separate Ubuntu VM with Docker isolation.  These
tests prove, mechanically, that the shipped Compose files and systemd units can
never reach production: no Docker socket, no production volumes, no secret
environment, host-local ports only.  They do not launch containers.
"""

import pytest
from pathlib import Path

from lab.workspace import check_command, check_compose_mounts, check_environment, \
    check_working_copy, IsolationReport


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
