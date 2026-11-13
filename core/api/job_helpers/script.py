"""Script task queueing, local generation and script results."""

import json
import os
from pathlib import Path

from ...configuration import load_character
from ...models import JobStatus
from ...script_schema import normalize_script
from ...state import task_queue


def _script_task_for(
        job,
        system_prompt: str,
        character: dict | None,
        revision: str | None = None) -> dict:
    return {"job_id": job.job_id, "task": "script", "priority": job.priority,
            "topic": job.topic, "task_id": None,
            "system_prompt": system_prompt, "character": character or {},
            "task_type": "text", "revision": revision}


def _enqueue_script_task(
        job_store,
        job,
        system_prompt: str = "",
        character: dict | None = None,
        revision: str | None = None) -> dict | None:
    workers = list(job_store.workers.values())
    has_text_worker = any(
        "text" in (worker.get("supported_tasks") or []) or worker.get("role") == "text"
        for worker in workers
    )
    from ...local_fallback import local_fallback_allowed
    if not has_text_worker and local_fallback_allowed():
        _generate_script_local(job_store, job, system_prompt, character)
        return None
    task = _script_task_for(job, system_prompt, character, revision)
    queued = task_queue.enqueue(task)
    job.script_task_id = queued["task_id"]
    job.active_task_id = queued["task_id"]
    job_store.repository.record_task(queued, "QUEUED")
    job_store.event(job, f"SCRIPT TASK {queued['task_id']} QUEUED")
    job_store.update(job, JobStatus.SCRIPT_QUEUED, "SCRIPT TASK QUEUED")
    return queued


def _load_character_prompt(job) -> tuple[str, dict | None]:
    character = load_character(
        Path(
            os.getenv(
                "CHARACTERS_ROOT",
                "characters")),
        job.character_id)
    system_prompt = (character.system_prompt or "").strip()
    character_dict = character.model_dump() if character else None
    return system_prompt, character_dict


def _generate_script_local(
        job_store,
        job,
        system_prompt: str,
        character: dict | None) -> None:
    # i.0.0.1.4 (#104): fail-closed even if a caller skips the gate — local
    # execution is dev/demo only.
    from ...local_fallback import require_no_local_execution
    require_no_local_execution("script")
    from ...script_agent import ScriptAgent
    script = ScriptAgent().generate_script(job.topic, system_prompt, character)
    job.script = normalize_script(script, job.topic)
    job.script_attempt = 0
    job.script_error = None
    job_store.update(job, JobStatus.SCRIPT_GENERATING,
                     "SCRIPT GENERATING (LOCAL FALLBACK)")
    _write_script_files(job_store, job)
    job_store.update(job, JobStatus.SCRIPT_PENDING_APPROVAL,
                     "SCRIPT GENERATED (LOCAL FALLBACK)")


def _write_script_files(store, job) -> None:
    if job.script:
        (store.root / job.job_id /
         "script.json").write_text(json.dumps(job.script, indent=2), encoding="utf-8")
        (store.root / job.job_id / "metadata.json").write_text(json.dumps({
            "title": job.script.get("title", job.topic), "language": "uk",
            "source": job.source, "character_id": job.character_id
        }, indent=2), encoding="utf-8")


def _handle_script_result(store, job, result, artifacts) -> None:
    success = result.get("success", False)
    task_id = result.get("task_id")

    if task_id and job.script_task_id == task_id:
        job.script_task_id = None
        job.assigned_worker = None

    if not success:
        error = result.get("error") or "UNKNOWN SCRIPT GENERATION ERROR"
        job.script_error = error
        store.event(job, f"SCRIPT TASK FAILED: {error}")
        job.script_attempt = (job.script_attempt or 0) + 1
        if job.script_attempt <= job.max_retries:
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(
                store,
                job,
                system_prompt,
                character,
                revision=job.revision)
        else:
            task_queue.dead_letter(
                {"job_id": job.job_id, "task_id": task_id, "task": "script"}, error)
            store.update(
                job,
                JobStatus.SCRIPT_FAILED,
                f"SCRIPT FAILED AFTER {job.script_attempt} ATTEMPTS")
        return

    script_artifact = next(
        (a for a in artifacts if a.get("kind") == "script"), None)
    if not script_artifact:
        store.event(job, "SCRIPT TASK COMPLETED BUT NO SCRIPT ARTIFACT")
        job.script_attempt = (job.script_attempt or 0) + 1
        if job.script_attempt <= job.max_retries:
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(
                store,
                job,
                system_prompt,
                character,
                revision=job.revision)
        else:
            store.update(
                job,
                JobStatus.SCRIPT_FAILED,
                "SCRIPT FAILED: NO ARTIFACT AFTER RETRIES")
            task_queue.dead_letter({"job_id": job.job_id,
                                    "task_id": task_id,
                                    "task": "script"},
                                   "NO SCRIPT ARTIFACT AFTER RETRIES")
        return

    try:
        import base64
        data = base64.b64decode(script_artifact["data_base64"])
        data_dict = json.loads(data.decode("utf-8"))
        job.script = normalize_script(data_dict, job.topic)
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        store.event(job, f"SCRIPT ARTIFACT MALFORMED: {exc}")
        job.script_attempt = (job.script_attempt or 0) + 1
        if job.script_attempt <= job.max_retries:
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(
                store,
                job,
                system_prompt,
                character,
                revision=job.revision)
        else:
            store.update(
                job,
                JobStatus.SCRIPT_FAILED,
                "SCRIPT FAILED: MALFORMED ARTIFACT AFTER RETRIES")
            task_queue.dead_letter({"job_id": job.job_id,
                                    "task_id": task_id,
                                    "task": "script"},
                                   f"MALFORMED ARTIFACT: {exc}")
        return

    job.active_task_id = None
    job.script_attempt = 0
    job.script_error = None
    _write_script_files(store, job)
    store.update(job, JobStatus.SCRIPT_PENDING_APPROVAL,
                 "SCRIPT GENERATED; AWAITING APPROVAL")
    if job.source.startswith("telegram:"):
        from ...pipeline import _send_script_approval_to_telegram
        try:
            _send_script_approval_to_telegram(job)
        except Exception as exc:
            store.event(
                job, f"TELEGRAM SCRIPT APPROVAL NOTIFICATION FAILED: {exc}")
