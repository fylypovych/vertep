"""Shared isolation rules for the in-process API test suite."""

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
    os.environ["JOB_ROOT"] = str(_TEST_STATE_ROOT / "jobs")
    os.environ["UPDATE_STATE_DIR"] = str(_TEST_STATE_ROOT / "update")
    os.environ["SYSTEM_STATE_BACKEND"] = "file"


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
        if task_queue.backend == "local":
            with task_queue._lock:
                task_queue._local.clear()
                task_queue._inflight.clear()
                task_queue._cancellations.clear()
                task_queue._dead_letters.clear()
                task_queue._sequence = 0

    reset()
    yield
    reset()
