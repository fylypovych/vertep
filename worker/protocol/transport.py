"""In-process worker transport using the same role entry point as Worker service."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock


class RoleWorkerTransport:
    def __init__(self, role: str, execute=None):
        self.role = role
        if execute is None:
            from worker.role_executor import execute_role_task
            execute = execute_role_task
        self.execute = execute
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow-worker")
        self._lock = Lock()
        self._active = None
        self._lease_id = None

    def is_available(self):
        with self._lock:
            return self._active is None or self._active.done()

    def submit(self, request, lease):
        with self._lock:
            if self._active is not None and not self._active.done():
                raise RuntimeError("Worker still executing previous attempt")
            self._lease_id = lease["lease_id"]
            payload = dict(request.payload)
            payload.update(task_id=request.task_id, lease_id=lease["lease_id"])
            self._active = self._pool.submit(self.execute, self.role, payload)
            return self._active

    def cancel(self, lease):
        with self._lock:
            if self._lease_id == lease["lease_id"] and self._active is not None:
                # A running synchronous provider cannot be killed by cancelling a
                # Future. Keep its slot occupied until actual completion.
                self._active.cancel()

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)
