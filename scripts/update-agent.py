import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def safe_git_env(root: Path) -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": str(root)}


def execute(command: list[str], root: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, cwd=root, env=safe_git_env(root), check=True,
                              capture_output=True, text=True,
                              timeout=int(os.getenv("UPDATE_TIMEOUT_SECONDS", "1800")))
    except subprocess.CalledProcessError as error:
        detail = ((error.stdout or "") + (error.stderr or "")).strip()[-4000:]
        raise RuntimeError(detail or f"Command failed with exit code {error.returncode}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Command timed out after {error.timeout} seconds") from error


def run(command: list[str], root: Path) -> str:
    result = execute(command, root)
    return (result.stdout + result.stderr).strip()


def git(root: Path, *args: str) -> str:
    return execute(["git", "-C", str(root), *args], root).stdout.strip()


def validate_remote(remote: str, allowed_host: str) -> None:
    if re.match(r"^[^@\s]+@([^:\s]+):.+$", remote):
        host = re.match(r"^[^@\s]+@([^:\s]+):.+$", remote).group(1)
    else:
        parsed = urlsplit(remote)
        if parsed.password or (parsed.scheme in {"http", "https"} and parsed.username):
            raise RuntimeError("Git remote must not contain embedded credentials")
        host = parsed.hostname or ""
    if host.lower() != allowed_host.lower():
        raise RuntimeError(f"Git remote host {host or 'unknown'} is not allowed")


def repository_status(root: Path, fetch: bool = True) -> dict:
    remote = git(root, "remote", "get-url", "origin")
    validate_remote(remote, os.getenv("UPDATE_ALLOWED_GIT_HOST", "github.com"))
    if fetch:
        git(root, "fetch", "--prune", "origin")
    current = git(root, "rev-parse", "HEAD")
    upstream = git(root, "rev-parse", "@{u}")
    counts = git(root, "rev-list", "--left-right", "--count", f"{current}...{upstream}").split()
    ahead, behind = (int(counts[0]), int(counts[1]))
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=no"))
    return {"current_revision": current, "remote_revision": upstream, "ahead": ahead, "behind": behind,
            "update_available": behind > 0, "dirty": dirty,
            "remote": re.sub(r"//[^/@:]+:[^/@]+@", "//***:***@", remote)}


def process_request(root: Path, state_dir: Path, request_path: Path) -> None:
    status_path = state_dir / "status.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_id = str(request.get("request_id", ""))
    action = request.get("action")
    if not re.fullmatch(r"[0-9a-f]{32}", request_id) or action not in {"check", "update"}:
        raise RuntimeError("Invalid update request")
    state = {"state": "RUNNING", "action": action, "request_id": request_id,
             "message": f"{action.title()} started", "updated_at": now(), "log": []}
    atomic_json(status_path, state)
    try:
        before = repository_status(root)
        state.update(before)
        state["log"].append(f"origin fetched; behind={before['behind']} ahead={before['ahead']}")
        atomic_json(status_path, state)
        if action == "update":
            if before["dirty"] and os.getenv("VERTEP_ALLOW_DIRTY_UPDATE", "false").lower() != "true":
                raise RuntimeError("Tracked local changes detected; update refused")
            if before["ahead"]:
                raise RuntimeError("Local branch is ahead of its GitHub upstream; update refused")
            if before["update_available"]:
                output = run(["/bin/bash", str(root / "scripts" / "vertep"), "update"], root)
                state["log"].extend(output.splitlines()[-100:])
            else:
                state["log"].append("Already up to date")
        after = repository_status(root, fetch=False)
        state.update(after)
        state.update({"state": "SUCCEEDED", "message": "Update completed" if action == "update"
                      else "Update check completed", "updated_at": now()})
    except Exception as error:
        state.update({"state": "FAILED", "message": str(error), "updated_at": now()})
        state.setdefault("log", []).append(str(error))
    finally:
        state["log"] = state.get("log", [])[-200:]
        atomic_json(status_path, state)
        request_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process trusted Vertep Web update requests")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    state_dir = args.state_dir.resolve()
    if not (root / ".git").exists() or not (root / "scripts" / "vertep").is_file():
        raise SystemExit("Vertep root is not a Git checkout")
    requests = state_dir / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    for request_path in sorted(requests.glob("*.json")):
        try:
            process_request(root, state_dir, request_path)
        except Exception as error:
            atomic_json(state_dir / "status.json", {"state": "FAILED", "action": None,
                        "request_id": None, "message": str(error), "updated_at": now(), "log": [str(error)]})
            request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
