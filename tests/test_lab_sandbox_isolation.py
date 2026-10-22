"""Negative integration tests for laboratory sandbox isolation."""
from pathlib import Path

def test_dockerfile_copies_lab_code():
    dockerfile = Path("deploy/lab/Dockerfile.sandbox")
    content = dockerfile.read_text(encoding="utf-8")
    assert "COPY ../../lab ./lab" in content, "Dockerfile must copy lab package"
    assert 'CMD ["python", "-m", "lab.cli", "status"]' in content

def test_sandbox_no_network_flags():
    dockerfile = Path("deploy/lab/Dockerfile.sandbox").read_text(encoding="utf-8")
    # Simple static checks: Dockerfile should not expose host Docker socket
    assert "docker_socket" not in dockerfile.lower()
    assert "/var/run/docker.sock" not in dockerfile
