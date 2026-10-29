from __future__ import annotations

from contextlib import asynccontextmanager
from core.api.job_helpers import _job_is_due
from core.api.job_helpers import _prepare_and_dispatch
from core.models import JobStatus
from core.node_registry import write_node_crl
from core.state import executor
from core.state import logger
from core.state import store
from core.state import task_queue
from core.system_state import dispatch_allowed
from pathlib import Path
import asyncio
import os
from core.runtime import _watchdog
from core.telegram.handlers import (_start_telegram_polling, _stop_telegram_polling, _recover_stale_update_operations, _reconcile_restart_operations)

@asynccontextmanager
async def lifespan(_app):
    # Issue 32: migrate ephemeral characters/brands/workflows to persistent storage on startup.
    try:
        from .persistent_data import ensure_persistent_user_data, persistent_characters_root, persistent_brands_root, workflows_persistent_root
        res = ensure_persistent_user_data()
        os.environ.setdefault("CHARACTERS_ROOT", str(persistent_characters_root()))
        os.environ.setdefault("BRANDS_ROOT", str(persistent_brands_root()))
        os.environ.setdefault("WORKFLOWS_ROOT", str(workflows_persistent_root()))
        # Re-point the already-imported registry if it was created with the old fallback.
        try:
            from .state import workflow_registry as _wr
            env_wf = os.getenv("WORKFLOWS_ROOT")
            if env_wf and str(_wr.root) != env_wf:
                _wr.root = Path(env_wf)
        except Exception:
            pass
        try:
            from .state import logger as _lg
            _lg.info("Persistent user-data ensured at startup: %s", res)
        except Exception:
            pass
    except Exception as _e:
        try:
            from .state import logger as _lg2
            _lg2.warning("Persistent user-data ensure failed: %s", _e)
        except Exception:
            pass
    if os.getenv("NODE_MTLS_REQUIRED", "false").lower() == "true":
        write_node_crl()
    # Issue #78: a provider backend chosen in the Web UI must survive restarts.
    try:
        from .provider_switch import apply_provider_overrides, reset_provider_registry
        if apply_provider_overrides():
            reset_provider_registry()
    except Exception as override_error:  # noqa: BLE001 - never block startup
        try:
            from .state import logger as _ov_log
            _ov_log.warning("Provider override apply failed: %s", override_error)
        except Exception:
            pass
    # JobStore resets interrupted scripts before Redis is constructed. Retire
    # their old leases before dispatching replacements, including durable owners.
    for task in task_queue.ready_tasks() + task_queue.inflight_tasks():
        job = store.jobs.get(task.get("job_id"))
        if not job:
            continue
        obsolete_script = task.get("task") == "script" and job.script_task_id != task["task_id"]
        obsolete_voice = task.get("task") == "voice" and task["task_id"] not in job.tts_active_task_ids
        recovering_publish = task.get("task") == "publish" and task["task_id"] in job.publish_task_ids
        if recovering_publish and task_queue.inflight_active(task["task_id"]):
            # A CORE restart does not prove the owning Publisher stopped.
            # Preserve its live lease; a new runtime identity or lease expiry
            # reconciles a genuinely interrupted Worker separately.
            continue
        if not (obsolete_script or obsolete_voice or recovering_publish):
            continue
        if recovering_publish:
            task_queue.release(task["task_id"])
        else:
            task_queue.discard(task["task_id"])
        owner = None
        for worker in store.workers.values():
            if worker.get("current_task") == task["task_id"]:
                owner = worker.get("node_name")
                worker["current_task"] = None
                worker["current_job"] = None
                worker["status"] = "OFFLINE"
                store.save_worker(worker)
        store.repository.record_task(task, "RELEASED", owner, error="task interrupted by CORE restart")
    for recovered_job in list(store.jobs.values()):
        if (recovered_job.status in {JobStatus.NEW, JobStatus.WAITING_FOR_SYSTEM}
                and dispatch_allowed() and _job_is_due(recovered_job)):
            if recovered_job.status == JobStatus.WAITING_FOR_SYSTEM:
                store.update(recovered_job, JobStatus.NEW, "SYSTEM RETURNED TO NORMAL")
            executor.submit(_prepare_and_dispatch, recovered_job)
    watchdog_task = asyncio.create_task(_watchdog())
    # Issue #174.3: an update operation whose host request was lost must reach
    # a terminal state after a restart, otherwise every retry is refused.
    try:
        recovered = _recover_stale_update_operations()
        if recovered:
            logger.warning("Recovered %d stale update operation(s) after restart", len(recovered))
    except Exception as recover_error:  # noqa: BLE001 - never block startup
        logger.warning("Update operation recovery failed: %s", recover_error)
    # Issue #174.4: a restart operation whose effect survived the process
    # restart must be finished and reported to the operator.
    try:
        finished = _reconcile_restart_operations()
        if finished:
            logger.warning("Finished %d restart operation(s) after restart", len(finished))
    except Exception as reconcile_error:  # noqa: BLE001 - never block startup
        logger.warning("Restart operation reconciliation failed: %s", reconcile_error)
    # Issue #78: model pulls live on background threads/worker commands and do
    # not survive a CORE restart — fail the orphaned ones so a retry is possible.
    try:
        from .pull_executor import reconcile_model_pull_operations
        recovered = reconcile_model_pull_operations()
        if recovered:
            logger.warning("Reconciled %d orphaned model operation(s) after restart", recovered)
    except Exception as reconcile_error:  # noqa: BLE001 - never block startup
        logger.warning("Model operation reconcile failed: %s", reconcile_error)
    # The polling service delivers the durable notifications that the
    # reconciliation above has queued, so it starts after the recovery.
    _start_telegram_polling()
    yield
    watchdog_task.cancel()
    _stop_telegram_polling()

