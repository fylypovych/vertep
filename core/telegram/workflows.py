"""Telegram selection and revision-aware approval of configured workflow DAGs."""

from adapters.telegram import TelegramAdapter
from core.workflow.runtime import workflow_runtime
from uuid import uuid4


def offer_workflows(chat_id, pending_data, character_id):
    runtime = workflow_runtime()
    if runtime is None:
        return None
    definitions = runtime.definitions()
    if not definitions:
        raise ValueError("Не налаштовано жодного шаблону workflow")
    pending_data["workflow_character"] = character_id
    pending_data["workflow_templates"] = list(definitions)
    pending_data.setdefault("workflow_source_key", f"telegram:{chat_id}:{pending_data['pending'].get('source_id') or uuid4()}")
    keyboard = {"inline_keyboard": [[{"text": definition["name"], "callback_data": f"select_workflow:{index}"}]
                                     for index, definition in enumerate(definitions.values())]}
    TelegramAdapter().send_message(chat_id, "Оберіть workflow перед початком генерації:", keyboard)
    return {"status": "workflow_selection", "templates": list(definitions)}


def select_workflow(callback, chat_id, selection, pending_data):
    adapter = TelegramAdapter()
    templates = pending_data.get("workflow_templates", [])
    if not selection.isdigit() or int(selection) >= len(templates):
        raise ValueError("Неактуальний вибір workflow")
    template = templates[int(selection)]
    pending = pending_data["pending"]
    params = {"topic": pending["text"], "character_id": pending_data["workflow_character"],
              "brand_id": pending_data["brand_id"], "telegram_chat_id": str(chat_id)}
    runtime = workflow_runtime()
    # Callback redelivery cannot create a second workflow for this pending input.
    job_id = pending_data.get("workflow_job_id")
    if job_id is None:
        job_id = runtime.create(template, params, source_key=pending_data.get("workflow_source_key"))
        pending_data["workflow_job_id"] = job_id
    adapter.answer_callback(str(callback.get("id", "")), "Workflow запущено")
    return send_status(chat_id, job_id)


def send_status(chat_id, job_id):
    runtime = workflow_runtime()
    job = runtime.engine._jobs.get(job_id)
    if job is None or str(job["params"].get("telegram_chat_id")) != str(chat_id):
        raise ValueError("Workflow не належить цьому чату")
    runtime.tick()
    status = runtime.engine.get_job_status(job_id)
    keyboard = [[{"text": "Оновити стан", "callback_data": f"wf:s:{job_id}"},
                 {"text": "Пауза", "callback_data": f"wf:p:{job_id}"}],
                [{"text": "Продовжити", "callback_data": f"wf:r:{job_id}"},
                 {"text": "Скасувати", "callback_data": f"wf:c:{job_id}"}]]
    for index, (task_id, task) in enumerate(job["tasks"].items()):
        if task["status"] == "COMPLETED" and task["approval"] == "PENDING":
            keyboard.append([{"text": f"Схвалити {task_id}", "callback_data": f"wf:a:{job_id}:{index}:{task.get('revision', 1)}"},
                             {"text": f"Відхилити {task_id}", "callback_data": f"wf:n:{job_id}:{index}:{task.get('revision', 1)}"}])
        definition = next(item for item in job["definition"]["tasks"] if item["id"] == task_id)
        if definition.get("type") in {"text_generation", "script"} and job["status"] in {"IN_PROGRESS", "PAUSED"}:
            keyboard.append([{"text": f"Правки {task_id}", "callback_data": f"wf:e:{job_id}:{index}:{task.get('revision', 1)}"}])
    from core.logging_config import secret_redact
    errors = "".join(f"\n{key}: {secret_redact(task['error'])[:300]}" for key, task in status["tasks"].items() if task.get("error"))
    TelegramAdapter().send_message(chat_id, f"Workflow {job_id}\nСтан: {status['status']}\nПрогрес: {status['progress']}%{errors}", {"inline_keyboard": keyboard})
    return {"job_id": job_id, **status}


def handle_action(callback, chat_id, payload):
    parts = payload.split(":")
    action, job_id = parts[:2]
    runtime = workflow_runtime()
    job = runtime.engine._jobs.get(job_id)
    if job is None or str(job["params"].get("telegram_chat_id")) != str(chat_id):
        raise ValueError("Workflow не належить цьому чату")
    if action in {"p", "r", "c"}:
        if action == "r":
            from core.system_state import dispatch_allowed
            runtime.engine.resume_job(job_id, allow_dispatch=dispatch_allowed())
        else:
            getattr(runtime.engine, {"p": "pause_job", "c": "cancel_job"}[action])(job_id)
    elif action in {"a", "n", "e"}:
        if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
            raise ValueError("Некоректна версія approval")
        tasks = list(job["tasks"])
        if int(parts[2]) >= len(tasks):
            raise ValueError("Неактуальна task")
        task_id = tasks[int(parts[2])]
        if action == "e":
            if job["tasks"][task_id].get("revision", 1) != int(parts[3]):
                raise ValueError("Неактуальна revision")
            if any(other.get("pending_revision", {}).get("chat_id") == str(chat_id) and key != job_id
                   for key, other in runtime.engine._jobs.items()):
                raise ValueError("Спочатку завершіть попередні правки")
            runtime.engine.pause_job(job_id)
            job["pending_revision"] = {"chat_id": str(chat_id), "task_id": task_id, "revision": int(parts[3])}
            runtime.engine.state.save_job(job_id, job)
            TelegramAdapter().answer_callback(str(callback.get("id", "")), "Workflow призупинено")
            return TelegramAdapter().send_message(chat_id, "Надішліть правки до тексту цього етапу.")
        runtime.engine.approve_task(job_id, task_id, action == "a", int(parts[3]))
    elif action != "s":
        raise ValueError("Невідома дія workflow")
    TelegramAdapter().answer_callback(str(callback.get("id", "")), "Виконано")
    return send_status(chat_id, job_id)


def apply_revision(chat_id, text):
    runtime = workflow_runtime()
    if runtime is None:
        return None
    for job_id, job in runtime.engine._jobs.items():
        pending = job.get("pending_revision", {})
        if pending.get("chat_id") != str(chat_id):
            continue
        if job["status"] != "PAUSED":
            raise ValueError("Workflow більше не очікує правки")
        definition = next(task for task in job["definition"]["tasks"] if task["id"] == pending["task_id"])
        inputs = {**definition.get("input", {}), "revision": text}
        runtime.engine.revise_task(job_id, pending["task_id"], inputs, pending["revision"])
        job.pop("pending_revision", None)
        runtime.engine.state.save_job(job_id, job)
        from core.system_state import dispatch_allowed
        runtime.engine.resume_job(job_id, allow_dispatch=dispatch_allowed())
        return send_status(chat_id, job_id)
    return None
