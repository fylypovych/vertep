"""Background work must finish before another test changes HTTP/auth mocks."""
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import _finish_executor_work


def test_drain_waits_for_busy_worker_even_when_the_other_worker_is_idle():
    started, release, drained = threading.Event(), threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        def slow_work():
            started.set()
            assert release.wait(5)
        work = pool.submit(slow_work)
        assert started.wait(5)
        def drain():
            _finish_executor_work(pool)
            drained.set()
        thread = threading.Thread(target=drain)
        thread.start()
        try:
            assert not drained.wait(0.1)
        finally:
            release.set()
            thread.join(timeout=5)
        assert drained.is_set() and work.done()
        work.result()
