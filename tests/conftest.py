"""Shared isolation rules for the in-process API test suite."""

import base64
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

# Issue i.0.0.1.68 (#109/#114): AdminAuthMiddleware is fail-closed — a backend
# with no configured admin user serves only the pre-setup allowlist, and every
# other route needs real credentials per request.  The suite therefore runs
# with a known admin password and its TestClient instances carry a default
# ``Authorization: Basic`` header, so ordinary API tests authenticate exactly
# like a real client.  Tests that assert missing/invalid credentials simply
# override the header (or monkeypatch ADMIN_PASSWORD, which invalidates this
# fixed credential for the duration of that test).
_TEST_ADMIN_PASSWORD = "vertep-test-admin-secret"
_TEST_AUTH_HEADER = "Basic " + base64.b64encode(
    f"admin:{_TEST_ADMIN_PASSWORD}".encode("utf-8")).decode("ascii")

from fastapi.testclient import TestClient as _TestClient  # noqa: E402

_orig_test_client_init = _TestClient.__init__


def _test_client_init_with_auth(self, app, **kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("Authorization", _TEST_AUTH_HEADER)
    kwargs["headers"] = headers
    _orig_test_client_init(self, app, **kwargs)


_TestClient.__init__ = _test_client_init_with_auth


_TEST_STATE_ROOT: Path | None = None


def pytest_configure(config):
    """Select writable, isolated host paths before test modules import core.app."""
    global _TEST_STATE_ROOT
    _TEST_STATE_ROOT = Path(tempfile.mkdtemp(prefix="vertep-tests-"))
    config_root_dir = _TEST_STATE_ROOT / "config"
    os.environ["JOB_ROOT"] = str(_TEST_STATE_ROOT / "jobs")
    os.environ["CONFIG_ROOT"] = str(config_root_dir)
    os.environ["STORAGE_ROOT"] = str(_TEST_STATE_ROOT / "storage")
    os.environ["UPDATE_STATE_DIR"] = str(_TEST_STATE_ROOT / "update")
    os.environ["SYSTEM_STATE_BACKEND"] = "file"
    os.environ["RATE_LIMIT_PER_MINUTE"] = "10000"
    os.environ["REDIS_URL"] = "redis://127.0.0.1:1/15"
    # Fail-closed middleware needs a non-empty admin password before it will
    # evaluate session/Basic credentials at all; pair it with the default
    # Authorization header installed on TestClient above.
    os.environ["ADMIN_PASSWORD"] = _TEST_ADMIN_PASSWORD
    os.environ.pop("USERS_JSON", None)
    # i.0.0.1.4 (#104): the unit suite is a dev/demo environment — local
    # fallback paths are active only when LOCAL_WORKER_FALLBACK=true AND
    # VERTEP_DEMO=true. Individual tests still gate with the former.
    os.environ["VERTEP_DEMO"] = "true"
    # Mark the isolated installation as "configured but auth-open" so the
    # First-Run 503 guard in AdminAuthMiddleware is bypassed for the whole
    # suite, while every on-disk artifact stays hermetic (never /data/config).
    # is_configured() (core/first_run.py) treats a present CONFIG_ROOT with a
    # completed_at installation.json as configured.
    config_root_dir.mkdir(parents=True, exist_ok=True)
    (config_root_dir / "users.json").write_text("{}", encoding="utf-8")
    (config_root_dir / "installation.json").write_text(
        json.dumps({"completed_at": "2024-01-01T00:00:00Z"}, ensure_ascii=False),
        encoding="utf-8",
    )


def pytest_unconfigure(config):
    if _TEST_STATE_ROOT is not None:
        shutil.rmtree(_TEST_STATE_ROOT, ignore_errors=True)


def _finish_executor_work(pool) -> None:
    """Fence both existing worker threads, including their completion callbacks."""
    barrier = threading.Barrier(3)
    futures = [pool.submit(barrier.wait, 30) for _ in range(2)]
    barrier.wait(timeout=30)
    for future in futures:
        future.result(timeout=30)


@pytest.fixture(autouse=True)
def reset_in_process_api_state(monkeypatch):
    """Do not leak process-local API state between otherwise isolated tests."""
    from core.state import executor, request_windows, setup_request_windows, store, task_queue
    from core.api.real_tests import _run_executor

    def finish_background_work() -> None:
        _finish_executor_work(_run_executor)
        _finish_executor_work(executor)

    def reset() -> None:
        finish_background_work()
        request_windows.clear()
        setup_request_windows.clear()
        store.jobs.clear()
        store.workers.clear()
        # OAuth refresh/revoke now persists across provider instances. Every
        # test is a separate installation, while restarts inside a test keep it.
        from core.first_run import ensure_secret_store, _write_encrypted_secrets
        secrets = ensure_secret_store()
        oauth = secrets.pop("publisher_oauth", {})
        before_secrets = dict(secrets)
        for platform in ("youtube", "tiktok", "facebook", "instagram", "threads"):
            for suffix in ("access_token", "refresh_token", "client_id", "client_secret"):
                secrets.pop(f"{platform}_{suffix}", None)
        for credential in oauth:
            secrets.pop(credential.lower(), None)
            secrets.pop(credential.lower().replace("_access_token", "_refresh_token"), None)
        if oauth or secrets != before_secrets:
            _write_encrypted_secrets(secrets)
        # File-backed repository persists workers/channels to disk; drop those
        # so a worker written by one test cannot leak into another test's fleet.
        repository = getattr(store, "repository", None)
        for attribute in ("worker_file", "channel_file"):
            path = getattr(repository, attribute, None)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if task_queue.backend == "redis":
            for key in task_queue._redis.scan_iter(match="vertep:*"):
                task_queue._redis.delete(key)
            task_queue._generation = 0
        else:
            with task_queue._lock:
                task_queue._local.clear()
                task_queue._inflight.clear()
                task_queue._cancellations.clear()
                task_queue._dead_letters.clear()
                task_queue._sequence = 0
                task_queue._generation = 0
        # Очищення стану Telegram
        from core.telegram.handlers import _telegram_pending_brands
        from core.telegram.handlers import _telegram_pending_character
        from core.telegram.handlers import _telegram_system_callbacks
        _telegram_pending_brands.clear()
        _telegram_pending_character.clear()
        _telegram_system_callbacks.clear()
        # Persistent monitor alerts must not leak between tests.
        from core.alert_store import reset_alert_store
        reset_alert_store()
        # System operating state and update status are durable files; clear them
        # so a non-NORMAL state or failed update left by one test cannot create
        # alerts in another test's /api/alerts response (Issue #192.4).
        from core.system_state import _path as _system_state_path
        from core.update_manager import state_root as _update_state_root
        from core.first_run import config_root as _config_root
        try:
            _system_state_path().unlink(missing_ok=True)
        except OSError:
            pass
        _update_root = _update_state_root()
        for name in ("status.json",):
            try:
                (_update_root / name).unlink(missing_ok=True)
            except OSError:
                pass
        requests_dir = _update_root / "requests"
        if requests_dir.is_dir():
            for req in requests_dir.glob("*.json"):
                try:
                    req.unlink()
                except OSError:
                    pass
        try:
            (_config_root() / "deployment-status.json").unlink(missing_ok=True)
        except OSError:
            pass
        # The durable node registry (config_root()/node-registry.json) is read by
        # workers() via registered_nodes(); a node enrolled by one test (e.g.
        # text-ready from test_full_media_harness) must not leak into another
        # test's /api/alerts WORKER_OFFLINE reconciliation (Issue #192.4).
        try:
            (_config_root() / "node-registry.json").unlink(missing_ok=True)
        except OSError:
            pass

    reset()
    yield
    # Finish effects while their test transports/auth are still installed;
    # restore patches before normal state cleanup touches storage.
    finish_background_work()
    monkeypatch.undo()
    reset()


_DISPOSABLE_HOST_CONSUMER = '''#!/usr/bin/env python3
"""Disposable Vertep host consumer with the production request contract."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _layout():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "release_layout", ROOT / "scripts" / "release-layout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _activation() -> dict:
    path = ROOT / ".activation.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _extract(package: Path, staging: Path) -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "safe-extract.py"),
                    str(package), str(staging)], check=True)
    if (staging / "VERSION").is_file():
        return staging
    children = sorted(item for item in staging.iterdir() if item.is_dir())
    if len(children) != 1:
        raise SystemExit("release payload is incomplete")
    return children[0]


def _prepare(payload: Path, version: str) -> None:
    releases = ROOT / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / version
    if release.exists():
        raise SystemExit(f"release already exists: {version}")
    try:
        _layout().prepare(ROOT, payload, version)
        return
    except OSError:
        shutil.rmtree(release, ignore_errors=True)
    shutil.copytree(payload, release)
    (release / ".release.json").write_text(
        json.dumps({"version": version, "sha256": ""}), encoding="utf-8")


def _activate_by_copy(version: str) -> None:
    release = ROOT / "releases" / version
    staged = ROOT / f".current.{os.getpid()}.tmp"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(release, staged)
    current = ROOT / "current"
    replaced = ROOT / f".current.{os.getpid()}.previous"
    if current.exists():
        os.replace(current, replaced)
    os.replace(staged, current)
    shutil.rmtree(replaced, ignore_errors=True)


def _activate(version: str, previous) -> None:
    release = ROOT / "releases" / version
    if not (release / "VERSION").is_file():
        raise SystemExit("release is missing")
    try:
        _layout().activate(ROOT, version)
    except OSError:
        _activate_by_copy(version)
    shutil.copyfile(ROOT / "current" / "VERSION", ROOT / "VERSION")
    (ROOT / ".activation.json").write_text(
        json.dumps({"current": version, "previous": previous}), encoding="utf-8")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "apply-update":
        package = Path(sys.argv[2])
        version = sys.argv[3]
        if (ROOT / "fail-apply").exists():
            raise SystemExit(7)
        staging = ROOT / f".staging.{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        payload = _extract(package, staging)
        _prepare(payload, version)
        _activate(version, _activation().get("current"))
        shutil.rmtree(staging, ignore_errors=True)
        if (ROOT / "fail-migration").exists():
            raise SystemExit(9)
    elif action == "rollback":
        previous = _activation().get("previous")
        if not previous:
            raise SystemExit("no immutable previous release is available")
        _activate(previous, None)
    elif action == "restart-runtime":
        (ROOT / "runtime-restarted").write_text("restarted", encoding="utf-8")
    else:
        raise SystemExit(f"unsupported action: {action}")


if __name__ == "__main__":
    main()
'''

_DISPOSABLE_BASH_CONSUMER = """#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
exec python3 "$ROOT/scripts/vertep-host.py" "$@"
"""


@pytest.fixture
def disposable_deployment():
    """Build a disposable Vertep installation with the host-consumer contract.

    The installation keeps immutable releases below ``releases/``, activates one
    of them as the active runtime instance and exposes its ``VERSION`` through
    ``current/VERSION``, exactly like the appliance release layout.  The host
    consumer performs the real instance switch, so the update agent observes a
    replaced runtime instead of a marker file.
    """

    def build(root: Path, current_version: str = "0.0.0.1") -> Path:
        root = Path(root)
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "vertep-host.py").write_text(_DISPOSABLE_HOST_CONSUMER, encoding="utf-8")
        if os.name == "nt":
            (scripts / "vertep").write_text(_DISPOSABLE_HOST_CONSUMER, encoding="utf-8")
        else:
            consumer = scripts / "vertep"
            consumer.write_text(_DISPOSABLE_BASH_CONSUMER, encoding="utf-8")
            consumer.chmod(0o755)
        shutil.copyfile(Path(__file__).parents[1] / "scripts" / "safe-extract.py",
                        scripts / "safe-extract.py")
        shutil.copyfile(Path(__file__).parents[1] / "scripts" / "release-layout.py",
                        scripts / "release-layout.py")
        release = root / "releases" / current_version
        release.mkdir(parents=True, exist_ok=True)
        (release / "VERSION").write_text(current_version, encoding="utf-8")
        (release / "application.txt").write_text(f"runtime {current_version}", encoding="utf-8")
        (release / ".release.json").write_text(
            json.dumps({"version": current_version, "sha256": ""}), encoding="utf-8")
        (root / "VERSION").write_text(current_version, encoding="utf-8")
        current = root / "current"
        if os.name != "nt":
            current.symlink_to(Path("releases") / current_version, target_is_directory=True)
        else:
            shutil.copytree(release, current)
        (root / ".activation.json").write_text(
            json.dumps({"current": current_version, "previous": None}), encoding="utf-8")
        return root

    return build
