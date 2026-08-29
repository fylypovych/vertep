"""Shared isolation rules for the in-process API test suite."""

import pytest


@pytest.fixture(autouse=True)
def reset_in_process_api_state():
    """Do not leak process-local API state between otherwise isolated tests."""
    from core.app import request_windows, setup_request_windows, store, task_queue

    def reset() -> None:
        request_windows.clear()
        setup_request_windows.clear()
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
