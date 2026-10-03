#!/usr/bin/env python3
"""Build, start and inspect the pinned MoneyPrinterTurbo runtime once.

Issue #122 P2 requires more than a static manifest: the image must actually be
built from the pinned tarball, started, and asked to prove its own snapshot. This
script performs that verification against a disposable container:

1. build the image from ``docker/moneyprinter/Dockerfile``;
2. read the immutable image digest of the freshly built image;
3. start it with a throwaway API key, task volume and the build digest;
4. require ``/health``, ``/runtime`` and ``/self-test`` to report the pinned
   commit, the bridge schema version, the dependency inventory and a media
   pipeline that really produced a file;
5. verify the reported digest equals the digest of the running image;
6. prove the auto-upload surface stayed disabled in the rendered config;
7. always remove the container and the anonymous volume.

Exit code 0 means every gate passed. Any missing gate, digest drift or disabled
upload fails closed with the reason printed to stderr.

Usage:
    python scripts/moneyprinter-runtime-check.py [--keep] [--timeout 600]

The script requires a working Docker daemon; it never talks to a production
runtime and never uses credentials from ``.env``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "moneyprinter" / "Dockerfile"
IMAGE_NAME = "vertep/moneyprinter-runtime-check"
WRAPPER_PORT = 8098
UPSTREAM_PORT = 8080


class CheckFailure(RuntimeError):
    """A verification gate failed; the runtime is not acceptable."""


def log(message: str) -> None:
    print(f"[moneyprinter-runtime-check] {message}", flush=True)


def docker(*args: str, capture: bool = True) -> str:
    executable = shutil.which("docker")
    if not executable:
        raise CheckFailure("docker is not available on this host")
    completed = subprocess.run(
        [executable, *args],
        capture_output=capture,
        text=True,
    )
    if completed.returncode != 0:
        raise CheckFailure(
            f"docker {' '.join(args)} failed: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return (completed.stdout or "").strip()


def docker_available() -> bool:
    try:
        docker("version", "--format", "{{.Server.Version}}")
    except CheckFailure:
        return False
    return True


def build_image(version: str) -> None:
    log(f"building {IMAGE_NAME} from {DOCKERFILE.relative_to(ROOT)}")
    docker(
        "build",
        "-f", str(DOCKERFILE),
        "-t", IMAGE_NAME,
        # The pins are read from the Dockerfile itself, so a verification run can
        # never drift from the image definition it claims to have checked.
        "--build-arg", f"VERTEP_VERSION={version}",
        "--build-arg", f"MPT_COMMIT={_build_arg('MPT_COMMIT')}",
        "--build-arg", f"MPT_TARBALL_SHA256={_build_arg('MPT_TARBALL_SHA256')}",
        "--build-arg", f"MPT_VERSION={_build_arg('MPT_VERSION')}",
        "--build-arg", f"BRIDGE_VERSION={_build_arg('BRIDGE_VERSION')}",
        "--build-arg", f"BRIDGE_SCHEMA_VERSION={_build_arg('BRIDGE_SCHEMA_VERSION')}",
        str(ROOT),
    )


def _build_arg(name: str) -> str:
    """Read a build argument the Dockerfile itself pins, so the two agree."""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"ARG {name}="):
            return stripped.split("=", 1)[1].strip().strip('"')
    raise CheckFailure(f"Dockerfile does not pin the build argument {name}")


def image_digest(reference: str) -> str:
    """Content identity of the image that was just built.

    A locally built image has no ``RepoDigests`` — those exist only for images pulled
    from or pushed to a registry — so the check would fail closed on every clean run
    before the runtime was ever asked anything. The local image ID *is* the
    content-addressed identity of exactly the image this script built, and it has the
    ``sha256:<64 hex>`` shape the runtime requires of a deployed image.
    """
    identity = local_image_id(reference)
    if not identity.startswith("sha256:") or len(identity) != len("sha256:") + 64:
        raise CheckFailure(f"image {reference} has no immutable content digest: {identity!r}")
    return identity


def registry_digest(reference: str) -> str:
    """Registry digest of the image, when the daemon knows one (informational)."""
    digest = docker("image", "inspect", "--format", "{{index .RepoDigests 0}}", reference)
    return digest if "@sha256:" in (digest or "") else ""


def local_image_id(reference: str) -> str:
    return docker("image", "inspect", "--format", "{{.Id}}", reference)


def start_container(name: str, digest: str, key_directory: Path) -> None:
    docker(
        "run", "-d", "--rm",
        "--name", name,
        # The runtime must prove itself through the wrapper port only; the
        # upstream API stays on loopback inside the container.
        "-p", f"127.0.0.1::{WRAPPER_PORT}",
        "-e", "MONEYPRINTER_API_KEY_FILE=/run/vertep-runtime-check/api_key",
        "-e", f"VERTEP_MONEYPRINTER_IMAGE_DIGEST={digest}",
        # The entrypoint reads `<mount>/api_key`, so the *directory* holding the
        # key is mounted: binding the key file onto the directory path itself would
        # make that path a file and the read would fail closed.
        "--mount", f"type=bind,source={key_directory},target=/run/vertep-runtime-check,readonly",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--network", "bridge",
        IMAGE_NAME,
    )


def mapped_port(name: str) -> int:
    mapping = docker("port", name, str(WRAPPER_PORT))
    host_part = mapping.rsplit(":", 1)[-1].strip()
    if not host_part.isdigit():
        raise CheckFailure(f"cannot resolve the wrapper port of {name}: {mapping!r}")
    return int(host_part)


def get_json(port: int, path: str, api_key: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"x-api-key": api_key},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_wrapper(port: int, api_key: str, deadline: float) -> dict:
    last_error = "wrapper did not answer yet"
    while time.monotonic() < deadline:
        try:
            return get_json(port, "/health", api_key)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(3)
    raise CheckFailure(f"wrapper /health never answered: {last_error}")


def verify_gate(health: dict) -> None:
    if health.get("status") != "ready":
        raise CheckFailure(
            f"wrapper is not ready: {health.get('reason')} {health.get('detail')}"
        )
    checks = health.get("checks") or {}
    if not checks.get("snapshot"):
        raise CheckFailure("wrapper reported ready without a verified snapshot")
    if not checks.get("upstream_auth_enforced"):
        raise CheckFailure("wrapper reported ready without a proven upstream authentication")
    missing = [
        field
        for field in ("video_subject", "video_script", "video_materials", "custom_audio_file")
        if field not in (checks.get("submit_schema") or [])
    ]
    if missing:
        raise CheckFailure(f"upstream submit schema is missing {missing}")
    media = checks.get("media_pipeline") or {}
    if not media.get("produced_bytes"):
        raise CheckFailure("the pinned media pipeline did not produce a file")


def verify_runtime(runtime: dict, image_digest_value: str) -> None:
    from adapters.providers.base import BRIDGE_SCHEMA_VERSION
    from adapters.providers.runtime_manifest import PINNED_UPSTREAM_COMMIT

    commit = runtime.get("upstream_commit")
    if commit != PINNED_UPSTREAM_COMMIT:
        raise CheckFailure(
            f"runtime reports upstream commit {commit!r}, expected {PINNED_UPSTREAM_COMMIT}"
        )
    if runtime.get("bridge_schema_version") != BRIDGE_SCHEMA_VERSION:
        raise CheckFailure(
            f"runtime reports bridge schema {runtime.get('bridge_schema_version')!r}, "
            f"expected {BRIDGE_SCHEMA_VERSION!r}"
        )
    if runtime.get("image_digest") != image_digest_value:
        raise CheckFailure(
            f"runtime reports image digest {runtime.get('image_digest')!r}, "
            f"built {image_digest_value!r}"
        )
    if not runtime.get("inventory_digest"):
        raise CheckFailure("runtime did not report a verified inventory digest")
    dependencies = runtime.get("dependencies") or []
    if not dependencies or runtime.get("dependency_count") != len(dependencies):
        raise CheckFailure("runtime did not publish its dependency inventory")
    for dependency in dependencies:
        if "==" not in dependency:
            raise CheckFailure(f"unpinned runtime dependency reported: {dependency}")


def verify_self_test(self_test: dict) -> None:
    if self_test.get("status") != "passed":
        raise CheckFailure(
            f"self-test did not pass: {self_test.get('reason')} {self_test.get('detail')}"
        )
    checks = self_test.get("checks") or {}
    if not (checks.get("media_pipeline") or {}).get("produced_bytes"):
        raise CheckFailure("self-test passed without a produced media file")


def verify_config_auto_upload(container: str) -> None:
    """Auto-upload must stay disabled in the config the runtime actually rendered."""
    config = docker("exec", container, "cat", "/opt/moneyprinter/config.toml")
    falsy = {"false", '""', "''", "[]", "{}", "0"}
    checked = 0
    for line in config.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("upload_post", "auto_upload")):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().startswith("upload_post_platforms"):
            continue
        checked += 1
        if value.strip() not in falsy:
            raise CheckFailure(f"auto-upload is enabled in the rendered config: {stripped}")
    if "upload_post_auto_upload" not in config:
        raise CheckFailure("the rendered config does not declare the auto-upload switch")
    if checked < 2:
        raise CheckFailure("the rendered config does not declare the auto-upload surface")


def collect_sbom(port: int, api_key: str) -> dict:
    """Read the SBOM the runtime itself publishes for its own image."""
    try:
        return get_json(port, "/sbom", api_key)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise CheckFailure(f"runtime did not publish an SBOM: {error}") from error


def write_evidence(directory: Path, image_digest_value: str, runtime: dict, sbom: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "runtime-snapshot.json").write_text(
        json.dumps({"image_digest": image_digest_value, **runtime}, indent=2),
        encoding="utf-8",
    )
    (directory / "sbom.json").write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    log(f"evidence written to {directory}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, default=600,
        help="seconds to wait for the wrapper to become ready (default: 600)",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="keep the built image instead of removing it (the container is always removed)",
    )
    parser.add_argument(
        "--evidence", default="",
        help="directory to write the runtime snapshot and SBOM evidence into",
    )
    arguments = parser.parse_args()

    if not DOCKERFILE.is_file():
        raise CheckFailure(f"missing {DOCKERFILE}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    name = f"moneyprinter-runtime-check-{secrets.token_hex(4)}"
    api_key = secrets.token_urlsafe(32)
    key_directory = Path(tempfile.mkdtemp(prefix="vertep-mpt-key-"))
    key_file = key_directory / "api_key"
    key_file.write_text(f"{api_key}\n", encoding="utf-8")
    container_started = False

    try:
        if not docker_available():
            raise CheckFailure("docker is not available on this host")
        build_image(version)
        digest = image_digest(IMAGE_NAME)
        log(f"built image content digest {digest}")
        registry = registry_digest(IMAGE_NAME)
        if registry:
            log(f"registry digest {registry}")

        start_container(name, digest, key_directory)
        container_started = True
        port = mapped_port(name)
        log(f"wrapper listens on 127.0.0.1:{port}")

        deadline = time.monotonic() + arguments.timeout
        health = wait_for_wrapper(port, api_key, deadline)
        verify_gate(health)

        runtime = get_json(port, "/runtime", api_key)
        verify_runtime(runtime, digest)

        self_test = get_json(port, "/self-test", api_key, timeout=120)
        verify_self_test(self_test)

        verify_config_auto_upload(name)
        sbom = collect_sbom(port, api_key)
        if not sbom.get("digest"):
            raise CheckFailure("the published SBOM has no digest")

        if arguments.evidence:
            write_evidence(Path(arguments.evidence), digest, runtime, sbom)

        log("all gates passed")
        return 0
    except CheckFailure as failure:
        print(f"[moneyprinter-runtime-check] FAILED: {failure}", file=sys.stderr)
        return 1
    finally:
        if container_started:
            subprocess.run(
                [shutil.which("docker") or "docker", "rm", "-f", name],
                capture_output=True,
                text=True,
            )
            log("disposable container removed")
        if not arguments.keep:
            subprocess.run(
                [shutil.which("docker") or "docker", "image", "rm", "-f", IMAGE_NAME],
                capture_output=True,
                text=True,
            )
            log("disposable image removed")
        shutil.rmtree(key_directory, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())