"""Shared isolation rules for the in-process API test suite."""

import base64
import json
import os
import shutil
import tempfile
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


@pytest.fixture(autouse=True)
def reset_in_process_api_state():
    """Do not leak process-local API state between otherwise isolated tests."""
    from core.app import executor, request_windows, setup_request_windows, store, task_queue

    def finish_background_work() -> None:
        barriers = [executor.submit(lambda: None) for _ in range(2)]
        for barrier in barriers:
            barrier.result(timeout=30)

    def reset() -> None:
        finish_background_work()
        request_windows.clear()
        setup_request_windows.clear()
        store.jobs.clear()
        store.workers.clear()
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
        from core.app import (_telegram_pending_brands, _telegram_pending_character,
                              _telegram_system_callbacks)
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
    reset()
