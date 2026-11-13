"""Telegram job callbacks: brand/character selection, approval, publish, storyboard, script and video."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import HTTPException

from adapters.telegram import TelegramAdapter
from core.api.job_helpers import (
    _prepare_and_dispatch,
    _enqueue_publish_task,
    _has_publisher_worker,
)
from core.artifacts import register_artifact
from core.configuration import (
    load_character,
    read_json,
)
from core.models import (
    JobStatus,
    StageName,
    StageStatus,
)
from core.orchestration import transition_stage
from core.pipeline import _finalize_video_regenerate
from core.state import (
    _telegram_pending_brands,
    _telegram_pending_character,
    executor,
    logger,
    store,
)
from core.telegram_store import is_admin_chat

from .common import _admin_chat_ids


_ABS_PATH_RE = re.compile(r"(?<![:/])(?<!\w)(?:[A-Za-z]:[\\/]|/)[^\s'\";)]*")


def _sanitize_creation_error(error: Exception) -> str:
    from core.logging_config import secret_redact
    text = str(error) or "невідома помилка"
    text = secret_redact(text)
    text = _ABS_PATH_RE.sub("[path]", text)
    return text[:200]


def _handle_brand_selection(callback: dict, chat_id: str, brand_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    pending = _telegram_pending_brands.get(chat_id)
    if not pending:
        return TelegramAdapter().answer_callback(callback_id, "Сесію закрито. Почніть спочатку.")
    TelegramAdapter().answer_callback(callback_id, f"Бренд обрано. Тепер оберіть персонажа.")
    _telegram_pending_brands.pop(chat_id, None)
    _telegram_pending_character[chat_id] = {"pending": pending, "brand_id": brand_id}
    return _send_character_selection(chat_id, brand_id)


def _send_character_selection(chat_id: str, brand_id: str) -> dict:
    characters_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    character_dirs = sorted(characters_root.glob("*"), key=lambda p: p.name)
    characters = []
    for char_dir in character_dirs:
        char_config_path = char_dir / "character.json"
        if char_config_path.exists():
            try:
                config = read_json(char_config_path)
                characters.append({"id": char_dir.name, "name": config.get("name", char_dir.name)})
            except (ValueError, OSError):
                continue
    if not characters:
        character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
        pending = _telegram_pending_character.get(chat_id, {}).get("pending", {})
        from .workflows import offer_workflows
        offered = offer_workflows(chat_id, _telegram_pending_character[chat_id], character_id)
        if offered is not None:
            return offered
        try:
            job = _create_job_from_telegram(pending, brand_id, character_id)
        except HTTPException as error:
            return TelegramAdapter().send_message(chat_id, str(error.detail))
        _telegram_pending_character.pop(chat_id, None)
        _queue_storyboard(job, chat_id)
        TelegramAdapter().send_message(chat_id, f"JOB {job.job_id}\nSTATUS: {job.status.value}\nПерсонажів не знайдено, використовується {character_id}. Генерую розкадровку…")
        return job.model_dump(mode="json")
    keyboard = {"inline_keyboard": [
        [{"text": f"👤 {char['name']}", "callback_data": f"select_character:{char['id']}"}] for char in characters
    ]}
    TelegramAdapter().send_message(chat_id, "Оберіть персонажа для цього завдання:", keyboard)
    return {"status": "character_selection", "brand_id": brand_id, "characters": [c["id"] for c in characters]}


def _handle_approve_job(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    if job.approval_status == "approved":
        return TelegramAdapter().answer_callback(callback_id, f"{job_id} вже схвалено.")
    job.approval_status = "approved"
    job.status = JobStatus.READY
    store.update(job, JobStatus.READY, "APPROVED via Telegram")
    channels = store.repository.list_channels(job.brand_id)
    active_channels = [ch for ch in channels if ch.enabled]
    if not active_channels:
        TelegramAdapter().send_message(
            chat_id, f"⚠️ {job_id} схвалено, але для бренду {job.brand_id} немає активних каналів.")
        return TelegramAdapter().answer_callback(callback_id, f"{job_id} схвалено (каналів немає)")
    keyboard = [[{"text": f"📤 {ch.channel_type}: {ch.target}", "callback_data": f"publish_channel:{ch.channel_id}:{job_id}"}] for ch in active_channels]
    keyboard.append([{"text": "📢 Опублікувати всюди", "callback_data": f"publish_all:{job_id}"}])
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(
                admin_chat, f"✅ {job_id} схвалено. Оберіть канали для публікації:",
                {"inline_keyboard": keyboard})
        except Exception:
            pass
    return TelegramAdapter().answer_callback(callback_id, f"{job_id} схвалено")


def _handle_reject_job(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    job.approval_status = "rejected"
    store.update(job, JobStatus.CANCELLED, "REJECTED via Telegram")
    return TelegramAdapter().answer_callback(callback_id, f"{job_id} відхилено")


def _handle_publish_channel(callback: dict, chat_id: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    channel_id, _, job_id = payload.partition(":")
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    channel = store.repository.get_channel(channel_id)
    if not channel:
        return TelegramAdapter().answer_callback(callback_id, "Канал не знайдено")
    result = _publish_to_channel(job, channel)
    if result.get("status") == "PUBLISHED":
        status_text = "✅ Опубліковано"
    elif result.get("status") == "QUEUED":
        status_text = "📤 Опубліковано в чергу"
    else:
        status_text = f"❌ Помилка: {result.get('error', 'невідомо')}"
    return TelegramAdapter().answer_callback(
        callback_id, f"{channel.channel_type} ({channel.target}): {status_text}")


def _handle_publish_all(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    channels = [ch for ch in store.repository.list_channels(job.brand_id) if ch.enabled]
    results = []
    for channel in channels:
        result = _publish_to_channel(job, channel)
        status = "✅" if result.get("status") == "PUBLISHED" else "❌"
        results.append(f"{status} {channel.channel_type}: {channel.target}")
    summary = "\n".join(results) or "Немає активних каналів"
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(
                admin_chat, f"📊 Підсумок публікації {job_id}:\n{summary}")
        except Exception:
            pass
    return TelegramAdapter().answer_callback(callback_id, "Публікацію завершено")


def _publish_to_channel(job, channel) -> dict:
    if job.approval_status != "approved":
        return {"status": "REJECTED", "error": "Job не схвалено для публікації"}
    if not _has_publisher_worker(store):
        return {"status": "NOT_CONFIGURED", "error": "Publisher Worker не зареєстровано; публікація неможлива без Publisher Worker"}
    receipt = (getattr(job, "publication_results", None) or {}).get(channel.channel_type)
    if receipt and receipt.get("status") == "PUBLISHED":
        return {"status": "PUBLISHED", "channel": channel.channel_type,
                "remote_id": receipt.get("remote_id"), "url": receipt.get("url"),
                "duplicate": True}
    in_flight = {value for value in (getattr(job, "publish_task_ids", None) or {}).values()}
    if channel.channel_type in in_flight:
        return {"status": "QUEUED", "channel": channel.channel_type, "duplicate": True}
    queued = _enqueue_publish_task(store, job, channel.channel_type)
    if queued is None:
        return {"status": "NOT_CONFIGURED", "error": f"Publisher для {channel.channel_type} не налаштовано"}
    if job.status != JobStatus.PUBLISHING:
        transition_stage(job, StageName.PUBLISH, StageStatus.RUNNING)
        store.update(job, JobStatus.PUBLISHING,
                                  f"PUBLISHING {channel.channel_type} via Publisher Worker")
    return {"status": "QUEUED", "task_id": queued["task_id"], "channel": channel.channel_type}


def _send_approval_request(job) -> None:
    text = (
        f"📋 Новий Job потребує затвердження\n"
        f"ID: {job.job_id}\n"
        f"Бренд: {job.brand_id}\n"
        f"Тема: {job.topic[:200]}\n"
        f"Джерело: {job.source}"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Схвалити", "callback_data": f"approve:{job.job_id}"},
         {"text": "❌ Відхилити", "callback_data": f"reject:{job.job_id}"}]
    ]}
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(admin_chat, text, keyboard)
        except Exception as error:
            logger.warning("Failed to send approval to %s: %s", admin_chat, error)


def _save_telegram_attachments(job, message: dict, root: Path) -> None:
    attachments = {key: message.get(key) for key in ("photo", "video", "document", "audio") if message.get(key)}
    if attachments:
        reference = root / job.job_id / "references" / "telegram.json"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(json.dumps(attachments, ensure_ascii=False, indent=2), encoding="utf-8")
        register_artifact(job, root, reference, "input", workflow="telegram:webhook")
        store.event(job, "TELEGRAM REFERENCES RECORDED")
    adapter = TelegramAdapter()
    file_ids = []
    if message.get("photo"):
        file_ids.append(message["photo"][-1].get("file_id"))
    for key in ("video", "document", "audio"):
        if message.get(key):
            file_ids.append(message[key].get("file_id"))
    if adapter.configured():
        for file_id in filter(None, file_ids):
            try:
                data, filename = adapter.download_file(file_id, int(os.getenv("TELEGRAM_MAX_FILE_BYTES", "26214400")))
                downloaded = root / job.job_id / "references" / Path(filename).name
                if downloaded.exists():
                    downloaded = downloaded.with_name(f"{downloaded.stem}-{hashlib.sha256(data).hexdigest()[:8]}{downloaded.suffix}")
                downloaded.write_bytes(data)
                register_artifact(job, root, downloaded, "input", workflow="telegram:download")
            except Exception as error:
                store.event(job, f"TELEGRAM DOWNLOAD FAILED: {error}")


def _create_job_from_telegram(pending: dict, brand_id: str, character_id: str | None = None):
    text = pending["text"]
    source_id = pending["source_id"]
    message = pending["message"]
    chat_id = message.get("chat", {}).get("id", "unknown")
    if character_id is None:
        character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
    source = f"telegram:{chat_id}:{source_id or 'unknown'}"
    if source_id and store.repository.has_telegram_update(chat_id, source_id):
        existing = next((job for job in store.jobs.values() if job.source == source), None)
        if existing:
            return existing
    for existing in store.jobs.values():
        if existing.source == source:
            return existing
    from core.system_state import jobs_may_be_created
    if not jobs_may_be_created():
        raise HTTPException(503, "Створення завдань тимчасово вимкнено поточним станом системи. Спробуйте пізніше.")
    try:
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(400, f"Unknown character: {character_id}") from error
    generation = character.generation if hasattr(character, 'generation') else {}
    workflow = generation.get("workflow") if isinstance(generation, dict) else getattr(character, 'workflow', None)
    job = store.create(text, character_id, int(os.getenv("TELEGRAM_DEFAULT_PRIORITY", "5")), source, workflow=workflow)
    job.brand_id = brand_id
    job.status = JobStatus.STORYBOARD_QUEUED
    job.approval_status = "pending"
    store.update(job, JobStatus.STORYBOARD_QUEUED, f"STORYBOARD QUEUED for brand {brand_id or 'default'}")
    if source_id:
        store.repository.record_telegram_update(chat_id, source_id, message)
    if pending.get("attachments"):
        _save_telegram_attachments(job, message, store.root)
    return job


def _handle_storyboard_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    raw = payload
    job_id, separator, rest = raw.partition(":")
    if not separator:
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    parts = rest.split(":")
    if not parts[0].isdigit():
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    version = int(parts[0])
    image_version = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    scene_index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    from core.storyboard import StoryboardService
    service = StoryboardService(store)
    try:
        if action == "sb_ok":
            job = service.approve(job_id, version, f"telegram:{chat_id}", expected_image_version=image_version)
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Розкадровку {version} схвалено. Pipeline продовжено."
        elif action == "sb_reject":
            service.reject(job_id, version, f"telegram:{chat_id}")
            text = f"❌ Розкадровку {version} відхилено."
        elif action == "sb_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            if job.storyboard_revision_chat_id and job.storyboard_revision_version:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Правки до розкадровки вже очікують на повідомлення")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            job.storyboard_image_revision_pending = False
            store.event(job, f"STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити."
        elif action == "sb_img_ok":
            job = service.approve_images(job_id, version, f"telegram:{chat_id}", expected_image_version=image_version)
            executor.submit(_prepare_and_dispatch, job)
            text = f"🖼️ Превʼю розкадровки {version} схвалено. Відео розблоковано."
        elif action == "sb_img_regen":
            service.request_image_revision(job_id, version, f"telegram:{chat_id}", None, None, expected_image_version=image_version)
            text = "🔁 Перегенеровую всі превʼю розкадровки."
            job = store.jobs.get(job_id)
            if job:
                sb = next((s for s in job.storyboards if s.version == version), None)
                if sb and all(s.image_artifact_id for s in sb.scenes):
                    sb.image_status = "ready"
        elif action == "sb_img_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            if job.storyboard_revision_chat_id and job.storyboard_image_revision_pending:
                from core.storyboard import StoryboardConflict
                raise StoryboardConflict("Правки до превʼю вже очікують на повідомлення")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            job.storyboard_image_revision_pending = True
            store.event(job, f"IMAGE STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть правки до превʼю одним повідомленням."
        elif action == "sb_img_scene":
            service.request_image_revision(job_id, version, f"telegram:{chat_id}", [scene_index] if scene_index else None, None, expected_image_version=image_version)
            text = f"🔁 Перегенеровую превʼю сцени {scene_index or ''}."
        else:
            executor.submit(_regenerate_storyboard_and_notify, job_id, version, chat_id)
            text = "🔄 Генерую нову версію розкадровки."
        return TelegramAdapter().answer_callback(callback_id, text)
    except KeyError:
        return TelegramAdapter().answer_callback(callback_id, "Job або розкадровку не знайдено")
    except Exception as error:
        from core.storyboard import StoryboardConflict
        if isinstance(error, StoryboardConflict):
            return TelegramAdapter().answer_callback(callback_id, str(error))
        raise


def _handle_script_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job_id = payload
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job не знайдено")
    from core.pipeline import approve_script, request_script_revision, regenerate_script
    from core.storyboard_telegram import render_script, script_keyboard
    try:
        if action == "sc_ok":
            job = approve_script(store, job, f"telegram:{chat_id}")
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Сценарій {job_id} схвалено."
        elif action == "sc_edit":
            if job.status not in {JobStatus.SCRIPT_PENDING_APPROVAL, JobStatus.SCRIPT_REVISION_REQUESTED}:
                raise ValueError(f"Сценарій не очікує правок (status={job.status.value})")
            if job.script_revision_pending and job.script_revision_chat_id:
                raise ValueError("Правки до сценарію вже очікують на повідомлення")
            job.script_revision_chat_id = chat_id
            job.script_revision_pending = True
            store.event(job, f"SCRIPT AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити у сценарії."
        elif action == "sc_regen":
            if job.status == JobStatus.SCRIPT_REVISION_REQUESTED:
                job = regenerate_script(store, job)
                executor.submit(_prepare_and_dispatch, job)
                text = "🔄 Перегенеровую сценарій…"
            elif job.status == JobStatus.SCRIPT_PENDING_APPROVAL:
                job.script = None
                job.version += 1
                store.transition(job, JobStatus.SCRIPT_REVISION_REQUESTED, f"SCRIPT REGENERATION REQUESTED by telegram:{chat_id}")
                executor.submit(_prepare_and_dispatch, job)
                text = "🔄 Перегенеровую сценарій…"
            else:
                text = f"Неможливо перегенерувати (status={job.status.value})"
        elif action == "sc_reject":
            store.update(job, JobStatus.CANCELLED, "SCRIPT REJECTED via Telegram")
            text = f"❌ Сценарій {job_id} відхилено."
        else:
            text = f"Невідома дія: {action}"
        return TelegramAdapter().answer_callback(callback_id, text)
    except ValueError as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))


def _handle_video_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    parts = payload.split(":", 1)
    job_id = parts[0]
    expected_version = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job не знайдено")
    source_parts = str(job.source or "").split(":")
    source_chat_id = source_parts[1] if len(source_parts) >= 2 and source_parts[0] == "telegram" else None
    if source_chat_id is not None and source_chat_id != chat_id:
        if action in {"vid_ok", "vid_reject", "vid_edit", "vid_regen"}:
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: chat не є власником job")
    if not is_admin_chat(chat_id) and source_chat_id != chat_id:
        return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
    from core.pipeline import approve_video, claim_video_regeneration, request_video_revision
    try:
        if action == "vid_ok":
            job = approve_video(store, job, f"telegram:{chat_id}", expected_version=expected_version)
            text = f"✅ Відео {job_id} схвалено."
        elif action == "vid_edit":
            if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
                raise ValueError(f"Відео не очікує правок (status={job.status.value})")
            if job.video_revision_pending and job.video_revision_chat_id:
                raise ValueError("Правки до відео вже очікують на повідомлення")
            job.video_revision_chat_id = chat_id
            job.video_revision_pending = True
            store.event(job, "VIDEO AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити у відео."
        elif action == "vid_regen":
            if job.video_regenerating:
                return TelegramAdapter().answer_callback(callback_id, "🔄 Відео вже генерується")
            if job.status == JobStatus.VIDEO_PENDING_APPROVAL:
                job = request_video_revision(store, job, "regenerate", f"telegram:{chat_id}")
            elif job.status != JobStatus.VIDEO_REVISION_REQUESTED:
                text = f"Неможливо перегенерувати (status={job.status.value})"
                return TelegramAdapter().answer_callback(callback_id, text)
            try:
                claim_video_regeneration(store, job, f"telegram:{chat_id}")
            except ValueError:
                return TelegramAdapter().answer_callback(callback_id, "🔄 Відео вже генерується")
            executor.submit(_finalize_video_regenerate, job)
            text = "🔄 Перегенеровую відео…"
        elif action == "vid_reject":
            if expected_version is not None and expected_version != job.active_video_version:
                raise ValueError(f"Stale video rejection: expected v{expected_version}, active is v{job.active_video_version}")
            store.update(job, JobStatus.CANCELLED, "VIDEO REJECTED via Telegram")
            text = f"❌ Відео {job_id} відхилено."
        else:
            text = f"Невідома дія: {action}"
        return TelegramAdapter().answer_callback(callback_id, text)
    except ValueError as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))








def _queue_storyboard(job, chat_id: str, revision: str | None = None) -> None:
    from core.storyboard import StoryboardService
    service = StoryboardService(store)
    service.store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    executor.submit(_generate_storyboard_and_notify, job.job_id, chat_id, revision)


def _generate_storyboard_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    from core.storyboard import StoryboardService
    try:
        StoryboardService(store).generate(job_id, revision)
    except Exception as error:
        logger.error("Storyboard generation failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося створити розкадровку {job_id}: {error}")
        except Exception:
            pass


def _regenerate_script_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    """Regenerate script after revision request from Telegram."""
    try:
        from core.pipeline import regenerate_script
        job = store.jobs.get(job_id)
        if not job:
            return
        regenerate_script(store, job)
    except Exception as error:
        logger.error("Script regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Помилка перегенерації сценарію {job_id}: {error}")
        except Exception:
            pass


def _regenerate_storyboard_and_notify(job_id: str, version: int, chat_id: str,
                                      revision: str | None = None) -> None:
    from core.storyboard import StoryboardService
    try:
        StoryboardService(store).regenerate(
            job_id, version, f"telegram:{chat_id}", revision
        )
    except Exception as error:
        logger.error("Storyboard regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося оновити розкадровку {job_id}: {error}")
        except Exception:
            pass
def _handle_character_selection(callback: dict, chat_id: str, character_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    pending_data = _telegram_pending_character.get(chat_id)
    if not pending_data:
        return TelegramAdapter().answer_callback(callback_id, "Сесію закрито. Почніть спочатку.")
    pending = pending_data["pending"]
    brand_id = pending_data["brand_id"]
    adapter = TelegramAdapter()
    try:
        adapter.answer_callback(callback_id, "Перевіряю персонажа…")
    except Exception:
        logger.warning("Telegram callback acknowledgement failed")
    try:
        load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
        from .workflows import offer_workflows
        offered = offer_workflows(chat_id, pending_data, character_id)
        if offered is not None:
            return offered
        job = _create_job_from_telegram(pending, brand_id, character_id)
    except Exception as error:
        logger.exception("Telegram Job creation failed", extra={"character_id": character_id})
        error_detail = _sanitize_creation_error(error)
        adapter.send_message(chat_id, f"❌ Не вдалося завершити створення завдання: {error_detail}\nПеревірте конфігурацію персонажа та сховище. Вибір збережено — можна повторити спробу.")
        return {"status": "creation_failed", "error": error_detail}
    _telegram_pending_character.pop(chat_id, None)
    try:
        adapter.send_message(chat_id, f"✅ Завдання {job.job_id} збережено. Воно доступне в адмінці у списку «Завдання».")
    except Exception:
        logger.warning("Telegram Job confirmation failed", extra={"job_id": job.job_id})
    try:
        _queue_storyboard(job, chat_id)
    except Exception:
        logger.exception("Telegram storyboard queue failed", extra={"job_id": job.job_id})
        adapter.send_message(chat_id, f"❌ Завдання {job.job_id} збережено, але обробку не вдалося запустити. Перевірте його стан в адмінці.")
    return job.model_dump(mode="json")
