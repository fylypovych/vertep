"""Shared isolation rules for the in-process API test suite."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest


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
        if task_queue.backend == "local":
            with task_queue._lock:
                task_queue._local.clear()
                task_queue._inflight.clear()
                task_queue._cancellations.clear()
                task_queue._dead_letters.clear()
                task_queue._sequence = 0
                task_queue._generation = 0
        # Очищення стану Telegram
        from core.app import _telegram_pending_brands, _telegram_pending_character
        _telegram_pending_brands.clear()
        _telegram_pending_character.clear()
        # Persistent monitor alerts must not leak between tests.
        from core.alert_store import reset_alert_store
        reset_alert_store()

    reset()
    yield
    reset()
