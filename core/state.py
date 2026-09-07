"""Centralized application state for the Vertep CORE web application.

This module owns the shared, mutable, process-local objects that the FastAPI
application and its domain routers operate on: the job store, task queue,
workflow registry, request rate-limit windows and the job-result lock table.

Objects here are mutated *in place* and are never rebound. Values that must be
rebound with ``global`` (for example ``last_maintenance`` and
``telegram_polling_service``) intentionally stay owned by ``core.app`` so their
lifetime remains tied to the FastAPI application lifecycle.
"""
import os
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from .pipeline import JobStore
from .queue import TaskQueue
from .logging_config import configure_logging
from .workflows import WorkflowRegistry

store = JobStore(os.getenv("JOB_ROOT", "jobs"))
executor = ThreadPoolExecutor(max_workers=2)
task_queue = TaskQueue()
logger = configure_logging("core")
workflow_registry = WorkflowRegistry(os.getenv("WORKFLOWS_ROOT", "workflows"))
request_windows: dict[str, deque[float]] = defaultdict(deque)
setup_request_windows: dict[str, deque[float]] = defaultdict(deque)
result_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_telegram_pending_brands: dict[str, dict] = {}
_telegram_pending_character: dict[str, dict] = {}
# Telegram service holder - mutates in place, never rebound.
# Owned by core.app lifespan; routes in core.api.telegram access .service.
class TelegramServiceHolder:
    def __init__(self) -> None:
        self.service: TelegramPollingService | None = None
telegram_service = TelegramServiceHolder()