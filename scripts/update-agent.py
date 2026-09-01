#!/usr/bin/env python3
"""Privileged, crash-recoverable agent for signed Vertep updates."""

import argparse
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def append_audit(state_dir: Path, event: dict) -> None:
    """Append a tamper-evident update event and durably chain it to the previous event."""
    import hashlib
    audit_path = state_dir / "audit.jsonl"
    previous_hash = "0" * 64
    try:
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            previous = json.loads(line)
            event_hash = previous.pop("event_hash")
            if previous.get("previous_hash") != previous_hash:
                raise RuntimeError("Update audit log hash chain is invalid")
            canonical_previous = json.dumps(
                previous, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if hashlib.sha256(canonical_previous.encode()).hexdigest() != event_hash:
                raise RuntimeError("Update audit log hash chain is invalid")
            previous_hash = event_hash
    except (FileNotFoundError, IndexError):
        pass
    except (OSError, ValueError, KeyError) as error:
        raise RuntimeError("Update audit log is unreadable") from error
    record = {**event, "timestamp": now(), "previous_hash": previous_hash}
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    record["event_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def run(command: list[str], root: Path) -> str:
    try:
        result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True,
                                timeout=int(os.getenv("UPDATE_TIMEOUT_SECONDS", "3600")))
    except subprocess.CalledProcessError as error:
        detail = ((error.stdout or "") + (error.stderr or "")).strip()[-4000:]
        raise RuntimeError(detail or f"Command failed with exit code {error.returncode}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Command timed out after {error.timeout} seconds") from error
    return (result.stdout + result.stderr).strip()


def core_json(path: str) -> dict:
    core_url = (os.getenv("VERTEP_CORE_URL") or os.getenv("CORE_URL")
                or "https://127.0.0.1:8443")
    url = core_url.rstrip("/") + path
    request = Request(url)
    internal_key = os.getenv("INTERNAL_API_KEY")
    password = os.getenv("ADMIN_PASSWORD")
    if internal_key:
        request.add_header("X-Vertep-Internal-Key", internal_key)
    elif password:
        credentials = f"{os.getenv('ADMIN_USER', 'admin')}:{password}"
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
    options = {"timeout": 10}
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        # The appliance proxy intentionally uses its private self-signed certificate on
        # loopback. This exception never applies to a remote CORE address.
        local_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        local_context.check_hostname = False
        local_context.verify_mode = ssl.CERT_NONE
        options["context"] = local_context
    with urlopen(request, **options) as response:
        return json.load(response)


def transition(state_dir: Path, state: dict, phase: str, message: str) -> None:
    from core.system_state import SystemState, set_system_state
    state.update({"phase": phase, "message": message, "updated_at": now()})
    state.setdefault("log", []).append(f"{now()} {message}")
    state["log"] = state["log"][-500:]
    atomic_json(state_dir / "status.json", state)
    append_audit(state_dir, {"operation_id": state.get("request_id"), "phase": phase,
                             "message": message, "action": state.get("action")})
    if phase in SystemState.__members__:
        set_system_state(SystemState[phase], message, state.get("request_id"), state_dir)


def wait_for_drain(state_dir: Path, state: dict) -> None:
    deadline = time.monotonic() + int(os.getenv("UPDATE_DRAIN_TIMEOUT_SECONDS", "86400"))
    transition(state_dir, state, "MAINTENANCE", "Maintenance mode; waiting for active jobs")
    while time.monotonic() < deadline:
        readiness = core_json("/api/system/update/readiness")
        if readiness.get("ready"):
            state["readiness"] = readiness
            transition(state_dir, state, "MAINTENANCE", "Workers drained and queue paused")
            return
        time.sleep(float(os.getenv("UPDATE_DRAIN_POLL_SECONDS", "5")))
    raise RuntimeError("Timed out waiting for jobs and workers to drain")


def recover_if_interrupted(root: Path, state_dir: Path) -> None:
    """Rollback an apply interrupted by power loss; maintenance itself is safe to resume."""
    status_path = state_dir / "status.json"
    try:
        state = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if state.get("state") != "RUNNING" or state.get("phase") not in {"UPDATING", "RECOVERING"}:
        return
    transition(state_dir, state, "RECOVERING", "Interrupted update detected; restoring last good release")
    output = run(["/bin/bash", str(root / "scripts" / "vertep"), "rollback"], root)
    state["log"].extend(output.splitlines()[-100:])
    state.update({"state": "ROLLED_BACK", "phase": "NORMAL", "message": "Rollback completed",
                  "updated_at": now()})
    atomic_json(status_path, state)
    from core.system_state import SystemState, set_system_state
    set_system_state(SystemState.NORMAL, "Previous release restored", state.get("request_id"), state_dir)


def process_request(root: Path, state_dir: Path, request_path: Path, skip_drain: bool = False) -> None:
    from core.system_state import SystemState, set_system_state
    from core.update_protocol import (download_package, fetch_manifest, validate_manifest,
                                      validate_replay_state, version_tuple)
    from core.update_lease import UpdateLease
    from core.update_trust import authorize_release_key, validate_root_metadata
    from core.version import application_version

    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_id, action = str(request.get("request_id", "")), request.get("action")
    if not re.fullmatch(r"[0-9a-f]{32}", request_id) or action not in {"check", "update"}:
        raise RuntimeError("Invalid update request")
    state = {"state": "RUNNING", "phase": "CHECKING", "action": action,
             "request_id": request_id, "message": "Checking signed release manifest",
             "updated_at": now(), "log": []}
    atomic_json(state_dir / "status.json", state)
    with UpdateLease(state_dir, request_id) as lease:
        state["fence_epoch"] = lease.fence_epoch
        atomic_json(state_dir / "status.json", state)
        try:
            manifest = fetch_manifest(os.getenv("UPDATE_CHANNEL", "stable"))
            requested_version = request.get("target_version")
            if requested_version and manifest.get("version") != requested_version:
                raise RuntimeError("Update server did not return the coordinated target version")
            public_key = Path(os.getenv(
                "UPDATE_PUBLIC_KEY", str(root / "installer" / "update-public.pem")))
            channel = os.getenv("UPDATE_CHANNEL", "stable")
            root_metadata_path = os.getenv("UPDATE_ROOT_METADATA")
            root_keys_path = os.getenv("UPDATE_ROOT_KEYS")
            if root_metadata_path or root_keys_path:
                if not root_metadata_path or not root_keys_path or not public_key.is_dir():
                    raise RuntimeError(
                        "Root metadata, root keys and release keyring must be configured together")
                metadata_document = json.loads(
                    Path(root_metadata_path).read_text(encoding="utf-8"))
                trusted_metadata_path = state_dir / "trusted-root.json"
                try:
                    trusted_metadata = json.loads(
                        trusted_metadata_path.read_text(encoding="utf-8"))
                    trusted_version = int(trusted_metadata["version"])
                    trusted_sha256 = str(trusted_metadata["metadata_sha256"])
                except FileNotFoundError:
                    trusted_version = 0
                    trusted_sha256 = None
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise RuntimeError("Stored root metadata state is unreadable") from error
                validated_metadata = validate_root_metadata(
                    metadata_document, Path(root_keys_path), trusted_version=trusted_version,
                    trusted_sha256=trusted_sha256)
                authorize_release_key(manifest, validated_metadata, public_key, channel)
                atomic_json(trusted_metadata_path, validated_metadata)
            elif os.getenv("REQUIRE_OFFLINE_ROOT", "false").lower() == "true":
                raise RuntimeError("Offline-root-signed update metadata is required")
            validate_manifest(manifest, public_key, expected_channel=channel)
            current = application_version()
            if version_tuple(manifest["version"]) < version_tuple(current):
                raise RuntimeError("Update server attempted to offer a downgrade")
            replay_path = state_dir / "trusted-release.json"
            try:
                trusted_release = json.loads(replay_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                trusted_release = None
            except (OSError, ValueError) as error:
                raise RuntimeError("Stored update replay state is unreadable") from error
            next_trusted_release = validate_replay_state(manifest, trusted_release)
            if next_trusted_release and next_trusted_release != trusted_release:
                atomic_json(replay_path, next_trusted_release)
            state.update({"current_version": current, "available_version": manifest["version"],
                          "required": bool(manifest.get("required", False)),
                          "update_available": version_tuple(manifest["version"]) > version_tuple(current)})
            transition(state_dir, state, "CHECKING", "Signed release manifest verified")
            if action == "update" and state["update_available"]:
                if not skip_drain:
                    wait_for_drain(state_dir, state)
                package = download_package(
                    manifest, state_dir / "packages" / f"vertep-{manifest['version']}.tar.gz")
                transition(state_dir, state, "UPDATING", "Backup and package installation started")
                output = run(["/bin/bash", str(root / "scripts" / "vertep"), "apply-update",
                              str(package), manifest["version"]], root)
                state["log"].extend(output.splitlines()[-200:])
                retention = os.getenv("UPDATE_RELEASE_RETENTION", "3")
                prune_output = run([sys.executable, str(root / "scripts" / "release-layout.py"),
                                    "prune", str(root), retention], root)
                state["log"].append(f"Immutable release retention: {prune_output}")
            state.update({"state": "SUCCEEDED", "phase": "NORMAL",
                          "message": "Update completed" if action == "update" else "Update check completed",
                          "updated_at": now()})
            set_system_state(SystemState.NORMAL, state["message"], request_id, state_dir)
        except Exception as error:
            state.setdefault("log", []).append(f"{now()} {error}")
            if state.get("phase") == "UPDATING":
                try:
                    transition(state_dir, state, "RECOVERING", "Health check failed; rolling back")
                    state["log"].extend(run(
                        ["/bin/bash", str(root / "scripts" / "vertep"), "rollback"], root
                    ).splitlines()[-100:])
                    state["state"] = "ROLLED_BACK"
                    set_system_state(SystemState.NORMAL, "Automatic rollback completed", request_id, state_dir)
                except Exception as rollback_error:
                    state.update({"state": "FAILED", "phase": "EMERGENCY"})
                    state["log"].append(f"{now()} rollback failed: {rollback_error}")
                    set_system_state(SystemState.EMERGENCY, "Update and rollback failed", request_id, state_dir)
            else:
                state.update({"state": "FAILED", "phase": "NORMAL"})
                set_system_state(SystemState.NORMAL, "Update stopped before installation", request_id, state_dir)
            state.update({"message": str(error), "updated_at": now()})
        finally:
            state["log"] = state.get("log", [])[-500:]
            atomic_json(state_dir / "status.json", state)
            request_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process signed Vertep update requests")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--skip-drain", action="store_true", help="Skip drain wait for worker nodes")
    args = parser.parse_args()
    root, state_dir = args.root.resolve(), args.state_dir.resolve()
    sys.path.insert(0, str(root))
    if not (root / "scripts" / "vertep").is_file():
        raise SystemExit("Invalid Vertep installation root")
    recover_if_interrupted(root, state_dir)
    requests = state_dir / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    for request_path in sorted(requests.glob("*.json")):
        try:
            process_request(root, state_dir, request_path, skip_drain=args.skip_drain)
        except Exception as error:
            atomic_json(state_dir / "status.json", {"state": "FAILED", "phase": "NORMAL",
                        "action": None, "request_id": None, "message": str(error),
                        "updated_at": now(), "log": [f"{now()} {error}"]})
            request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
