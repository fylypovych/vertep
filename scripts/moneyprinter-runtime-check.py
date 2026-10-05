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
import os
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
# This gate is executed as ``python scripts/moneyprinter-runtime-check.py``, which puts
# ``scripts/`` and not the repository root on the path, so the bridge contract could not
# be imported. Every other repo script does the same bootstrap.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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
    """Registry digest of the image, when the daemon knows one (informational).

    ``{{index .RepoDigests 0}}`` makes the daemon fail on an empty ``RepoDigests``,
    which is the normal state of a locally built image, so the slice is read as JSON
    and an image without one simply reports nothing.
    """
    raw = docker("image", "inspect", "--format", "{{json .RepoDigests}}", reference)
    try:
        entries = json.loads(raw or "[]") or []
    except json.JSONDecodeError:
        return ""
    for entry in entries:
        if isinstance(entry, str) and "@sha256:" in entry:
            return entry
    return ""


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


def describe_refusal(path: str, status: int, body: str) -> str:
    """Turn a wrapper refusal into the reason the runtime itself reported.

    The wrapper answers every failed gate with a stable reason code and the checks it had
    already completed. Without them a bare ``HTTP Error 503`` hides which gate refused,
    so the CI log cannot say anything about the runtime.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return f"HTTP {status} from {path}: {body.strip()[:400]}"
    if not isinstance(payload, dict):
        return f"HTTP {status} from {path}: {body.strip()[:400]}"
    parts = [f"HTTP {status} from {path}"]
    for key in ("reason", "detail", "status"):
        value = payload.get(key)
        if value:
            parts.append(f"{key}={value}")
    completed = sorted(payload.get("checks") or {})
    if completed:
        parts.append(f"completed checks={','.join(completed)}")
    return ": ".join(parts)


def get_json(port: int, path: str, api_key: str, timeout: int = 20, *,
              retry_refusal: bool = False) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"x-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        # Readiness polling treats a refusal as "not yet": /health fails closed with 503
        # while the runtime is still starting. Every other gate has to explain itself.
        if retry_refusal:
            raise
        raise CheckFailure(describe_refusal(path, error.code, body)) from error


def wait_for_wrapper(port: int, api_key: str, deadline: float) -> dict:
    last_error = "wrapper did not answer yet"
    while time.monotonic() < deadline:
        try:
            return get_json(port, "/health", api_key, retry_refusal=True)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(3)
    raise CheckFailure(f"wrapper /health never answered: {last_error}")


# Keys this gate reads from the wrapper ``/health`` report. The media proof is not one of
# them: ``/health`` is a cheap readiness report, and the produced media file is proven by
# the self-test gate below. Reading a field the report never publishes would fail a
# perfectly healthy runtime for a reason it cannot fix.
HEALTH_GATE_KEYS = ("snapshot", "upstream_auth_enforced", "submit_schema")


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
    # The expected fields come from the bridge contract, not from a copy made here: a
    # hand-kept subset silently accepted a runtime that had stopped accepting the
    # script, the staged materials or the approved voice (§9.3), and a runtime that
    # stopped accepting a compose field would apply its own default to the approved
    # render without failing the submit (§9.4).
    from adapters.providers.base import FIXED_SUBMIT_FIELDS

    proved = checks.get("submit_schema") or ()
    proved_names = set(proved)
    missing = [field for field in FIXED_SUBMIT_FIELDS if field not in proved_names]
    if missing:
        raise CheckFailure(
            f"upstream submit schema is missing {missing}; the bridge submits "
            f"{', '.join(FIXED_SUBMIT_FIELDS)}"
        )


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
    # ``check_media_pipeline`` publishes the byte count and the duration it measured on
    # the file the pinned compose path produced; both have to be real for the §9.12 media
    # gate to mean anything.
    media = checks.get("media_pipeline") or {}
    if not media.get("bytes"):
        raise CheckFailure(
            f"self-test passed without a produced media file: {media!r}"
        )
    if not media.get("duration_seconds"):
        raise CheckFailure(
            f"self-test passed with an undecodable media file: {media!r}"
        )
    # §9.3 rows 2–4 and 8 are the runtime's own responsibility: materials arrive as
    # pre-cut clips in scene order, each carrying its approved scene duration, and the
    # compose uses the job aspect. A self-test that no longer proves the timeline would
    # let a runtime that reorders or re-times scenes pass the gate unnoticed.
    scenes = media.get("scenes") or []
    expected_order = media.get("expected_scene_order") or []
    if not scenes or not expected_order:
        raise CheckFailure(
            f"self-test passed without a proven scene timeline: {media!r}"
        )
    if [scene.get("label") for scene in scenes] != expected_order:
        raise CheckFailure(
            f"pinned compose rendered scenes out of order: "
            f"{[scene.get('label') for scene in scenes]} != {expected_order}"
        )
    if media.get("scene_order") != expected_order:
        raise CheckFailure(
            f"self-test reported a different scene order than it measured: "
            f"{media.get('scene_order')!r} != {expected_order!r}"
        )
    for scene in scenes:
        if not scene.get("seconds") or not scene.get("at_seconds"):
            raise CheckFailure(f"scene timeline entry has no duration: {scene!r}")
        if scene["at_seconds"] <= 0 or scene["at_seconds"] >= media["duration_seconds"]:
            raise CheckFailure(
                f"scene {scene.get('label')!r} lies outside the rendered timeline: {scene!r}"
            )
        drift = max(
            abs(actual - expected)
            for actual, expected in zip(scene.get("mean_colour") or [],
                                       scene.get("expected_colour") or [])
        ) if scene.get("mean_colour") and scene.get("expected_colour") else None
        if drift is None or drift > 48:
            raise CheckFailure(
                f"scene {scene.get('label')!r} does not match its approved clip: {scene!r}"
            )
    if not media.get("aspect") or not media.get("fit_mode"):
        raise CheckFailure(f"self-test did not report the compose aspect/fit mode: {media!r}")
    # The compose helper in isolation proves nothing about the attempt the factory makes.
    # §9.4 acceptance is a real submit through the wrapper's own upload/submit routes, an
    # approved voice staged inside the accepted task, an upstream task that finished and a
    # download that decodes to the job aspect — a runtime that would narrate on its own or
    # publish by itself cannot pass this gate.
    route = checks.get("submit_route")
    if not isinstance(route, dict) or not route:
        raise CheckFailure(f"self-test passed without a proven submit route: {route!r}")
    # ``1`` is the pinned runtime's own "task finished" state.
    if route.get("state") != 1:
        raise CheckFailure(f"the submit route did not finish an upstream task: {route!r}")
    task_id = route.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise CheckFailure(f"the submit route produced no upstream task: {route!r}")
    if not route.get("submit_key"):
        raise CheckFailure(f"the submit route used no durable submit key: {route!r}")
    if route.get("voice_staged") is not True:
        raise CheckFailure(
            f"the submit route did not prove the approved voice inside the task: {route!r}"
        )
    if not route.get("bytes"):
        raise CheckFailure(f"the submit route downloaded no media bytes: {route!r}")
    if not route.get("duration_seconds"):
        raise CheckFailure(f"the submit route downloaded no media duration: {route!r}")
    width, height = route.get("width") or 0, route.get("height") or 0
    if not width or not height:
        raise CheckFailure(f"the submit route reported no video stream size: {route!r}")
    if not 1.6 <= height / width <= 1.95:
        raise CheckFailure(
            f"the submit route rendered outside the submitted 9:16 aspect: "
            f"{width}x{height}"
        )
    drift = max(
        abs(actual - expected)
        for actual, expected in zip(route.get("mean_colour") or [],
                                   route.get("expected_colour") or [])
    ) if route.get("mean_colour") and route.get("expected_colour") else None
    if drift is None or drift > 48:
        raise CheckFailure(
            f"the submit route did not render the submitted scene: {route!r}"
        )


# Markers of a setting that can make the pinned runtime publish on its own: an enable
# switch, a credential or a configured platform. Only settings carrying one of these must
# be empty in the rendered config. ``upload_post_youtube_privacy_status`` and
# ``_made_for_kids`` are account visibility labels — a locked non-empty value there
# cannot publish anything, and refusing it would be refusing a value the configuration
# lock deliberately pins.
PUBLICATION_MARKERS = (
    "enabled",
    "auto_upload",
    "api_key",
    "username",
    "password",
    "token",
    "cookie",
    "secret",
    "platform",
    "url",
)


def _publication_switches(config: str) -> list[tuple[str, str]]:
    """Every ``key = value`` pair in the config that could enable publishing."""
    switches: list[tuple[str, str]] = []
    for line in config.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("upload_post", "auto_upload")) or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not any(marker in key for marker in PUBLICATION_MARKERS):
            continue
        switches.append((key, value.strip()))
    return switches


def verify_config_auto_upload(container: str) -> None:
    """Auto-upload must stay disabled in the config the runtime actually rendered."""
    config = docker("exec", container, "cat", "/opt/moneyprinter/config.toml")
    falsy = {"false", '""', "''", "[]", "{}", "0"}
    switches = _publication_switches(config)
    for key, value in switches:
        if value not in falsy:
            raise CheckFailure(f"auto-upload is enabled in the rendered config: {key} = {value}")
    if "upload_post_auto_upload" not in config:
        raise CheckFailure("the rendered config does not declare the auto-upload switch")
    if not {key for key, _ in switches} >= {
        "upload_post_enabled",
        "upload_post_auto_upload",
        "upload_post_api_key",
        "upload_post_platforms",
    }:
        raise CheckFailure(
            "the rendered config does not declare the auto-upload surface: "
            f"{sorted(key for key, _ in switches)}"
        )


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


def container_exit_status(name: str) -> str:
    """Exit code and state of the container, empty while it still runs."""
    state = docker("inspect", "--format", "{{.State.Status}}", name).strip()
    code = docker("inspect", "--format", "{{.State.ExitCode}}", name).strip()
    return f"{state} (exit code {code})"


def report_container_log(name: str, lines: int = 60) -> None:
    """Print the container log before it is discarded, so a failure is diagnosable."""
    log(f"container state {container_exit_status(name)}")
    output = subprocess.run(
        [shutil.which("docker") or "docker", "logs", "--tail", str(lines), name],
        capture_output=True,
        text=True,
    )
    for stream in (output.stdout, output.stderr):
        for line in stream.splitlines():
            if line.strip():
                print(f"[moneyprinter-runtime-check] container | {line}", file=sys.stderr)


def write_api_key(key_directory: Path) -> tuple[Path, str]:
    """Write the ephemeral key so the runtime's own uid can read the mount.

    The key never leaves the disposable container, but the runtime deliberately runs
    as its own non-root uid, which cannot read a 0700 host directory. Without read
    access the entrypoint fails closed before it ever listens, so the mount is made
    traversable and the short-lived key readable by that uid.
    """
    api_key = secrets.token_urlsafe(32)
    key_file = key_directory / "api_key"
    key_file.write_text(f"{api_key}\n", encoding="utf-8")
    os.chmod(key_directory, 0o755)
    os.chmod(key_file, 0o444)
    return key_file, api_key


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
    api_key = ""
    key_directory = Path(tempfile.mkdtemp(prefix="vertep-mpt-key-"))
    _key_file, api_key = write_api_key(key_directory)
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

        # The self-test renders the §9.12 media timeline and drives a real submit → status
        # → download route, so it is given the requested gate budget instead of a fixed
        # 120s: cutting the read short would report a transport timeout for a runtime that
        # is still proving itself, and hide which gate was running.
        self_test = get_json(port, "/self-test", api_key,
                             timeout=max(300, arguments.timeout))
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
        if container_started:
            # The container is about to be removed, so its log is the only evidence left
            # of why the runtime never answered.
            report_container_log(name)
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