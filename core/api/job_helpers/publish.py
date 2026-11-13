"""Publish tasks, public video URLs, receipts and publish results."""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from ...logging_config import secret_redact
from ...models import JobStatus
from ...state import task_queue


def _has_publisher_worker(job_store) -> bool:
    return any("publish" in (worker.get("supported_tasks") or []) or worker.get(
        "role") == "publisher" for worker in job_store.workers.values())


def _public_video_signature(job_id: str, version: int, expires: int) -> str:
    secret = os.getenv("SESSION_SECRET", "")
    message = f"{job_id}:{version}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest() if secret else ""


def _public_video_url(job) -> dict:
    """Time-limited address of exactly one video version, or nothing when CORE is not public."""
    from ...publishing_settings import publishing_settings
    base = publishing_settings()["public_url"] or os.getenv("PUBLIC_URL", "").rstrip("/")
    version = job.active_video_version
    if not base or version is None:
        return {}
    expires = int(time.time()) + int(os.getenv("PUBLIC_VIDEO_LINK_TTL_SECONDS", "21600"))
    signature = _public_video_signature(job.job_id, version, expires)
    if not signature:
        return {}
    return {"video_url": f"{base}/api/public/videos/{job.job_id}/{version}/{expires}/{signature}.mp4"}


def _publish_task_for(job, channel: str) -> dict:
    """Build a publish task carrying the durable delivery contract.

    The contract pins the exact video version (path + sha256 + size) that the
    Publisher Worker must upload, so a retry after a lost ack or a restart
    re-publishes the same artifact instead of a stale/different one.  The
    contract also declares the receipt correlation keys (job_id, version,
    channel) that CORE uses to match an incoming receipt back to the intent.
    """
    delivery = _publish_delivery_contract(job)
    intent = {
        "job_id": job.job_id,
        "channel": channel,
        "video_version": delivery.get("version"),
        "video_sha256": delivery.get("sha256"),
        "video_size": delivery.get("size"),
        "attempt": (job.publish_retry_count.get(channel, 0) + 1),
    }
    job.publish_intent[channel] = intent
    # Threads downloads the video itself; other channels receive the file.
    public_link = _public_video_url(job) if channel == "threads" else {}
    if channel == "telegram":
        from ...publishing_settings import publishing_settings
        public_link = {"telegram_chat_id": publishing_settings()["telegram_channel_id"]}
    return {"job_id": job.job_id,
            "task": "publish",
            "priority": job.priority,
            "channel": channel,
            "topic": job.topic,
            "task_id": None,
            "video_path": job.output_path or "",
            "delivery_contract": delivery,
            "publish_intent": intent,
            "metadata": {"job_id": job.job_id,
                         "topic": job.topic,
                         "character_id": job.character_id,
                         "brand_id": job.brand_id,
                         **(job.script or {}),
                         **((job.script or {}).get("platforms",
                                                   {}).get(channel) or {}),
                         **public_link},
            "task_type": "publish"}


def _publish_delivery_contract(job) -> dict:
    """Compute the immutable delivery contract for the current job video.

    Returns a dict with the version, path, sha256, size and mime type of the
    artifact the Publisher Worker must upload.  Missing files degrade to a
    best-effort contract (path only) so a retry still targets the same path.
    """
    path = job.output_path or ""
    contract = {"version": job.active_video_version, "path": path,
                "mime_type": "video/mp4"}
    if path and os.path.isfile(path):
        try:
            data = Path(path).read_bytes()
            contract["sha256"] = hashlib.sha256(data).hexdigest()
            contract["size"] = len(data)
        except OSError:
            contract["sha256"] = None
            contract["size"] = None
    else:
        contract["sha256"] = None
        contract["size"] = None
    job.publish_delivery_contract = contract
    return contract


def _match_publish_receipt(
        job, channel: str, receipt: dict) -> tuple[str, str | None]:
    """Correlate a receipt with the durable publish intent.

    Returns (decision, reason) where decision is one of:
      - "accept"   — receipt matches the intent (or no intent was recorded yet,
                     e.g. a direct test/helper call) and the channel is not yet published
      - "skip"     — channel already published with an identical version (idempotent)
      - "reject"   — receipt does not match the intent (wrong version/owner/lease)
    """
    intent = job.publish_intent.get(channel)
    if intent:
        receipt_version = receipt.get(
            "video_version") or receipt.get("version")
        if receipt_version is not None and intent.get("video_version") is not None \
                and str(receipt_version) != str(intent["video_version"]):
            return "reject", (f"Receipt video_version {receipt_version!r} does not match "
                              f"intent version {intent['video_version']!r}")
        receipt_sha = receipt.get("video_sha256") or receipt.get("sha256")
        if receipt_sha and intent.get(
                "video_sha256") and receipt_sha != intent["video_sha256"]:
            return "reject", "Receipt video_sha256 does not match delivery contract"
    if channel in job.published_channels:
        return "skip", "Channel already published (idempotent)"
    return "accept", None


def _enqueue_publish_task(
        job_store,
        job,
        channel: str,
        delay: float = 0) -> dict | None:
    has_publisher = _has_publisher_worker(job_store)
    from ...local_fallback import local_fallback_allowed
    if not has_publisher and local_fallback_allowed():
        _publish_local(job_store, job, channel)
        return None
    task = _publish_task_for(job, channel)
    if delay:
        task["not_before"] = time.time() + delay
    queued = task_queue.enqueue(task)
    job.publish_task_ids[queued["task_id"]] = channel
    job_store.repository.record_task(queued, "QUEUED")
    job_store.event(
        job, f"PUBLISH TASK {queued['task_id']} QUEUED FOR {channel}")
    return queued


def _publish_local(job_store, job, channel: str) -> dict:
    # i.0.0.1.4 (#104): fail-closed even if a caller skips the gate — local
    # execution is dev/demo only.
    from ...local_fallback import require_no_local_execution
    require_no_local_execution("publish")
    from ...pipeline import _do_publish_single
    result = _do_publish_single(job, channel)
    # Local (dev/demo) publication crosses the same operator-visible boundary
    # as a worker receipt, so mask secret-shaped values before storing it.
    result = _redact_payload(result)
    job.publication_results[channel] = result
    if result.get("status") == "PUBLISHED":
        channel_str = ",".join(result.get("channels", [channel]))
        job.published_to.append(channel_str)
    elif result.get("status") == "NOT_CONFIGURED":
        job.publish_error = result.get("error", "NOT_CONFIGURED")
    job.publish_attempt = 0
    job_store.event(job, f"PUBLISHED {channel}: {result.get('status')}")
    return result


def _is_mock_receipt(receipt: dict) -> bool:
    upload = receipt.get("upload")
    mode = str(upload.get("mode") or "") if isinstance(upload, dict) else ""
    return mode.startswith("mock") or "example.invalid" in str(receipt.get("url") or "")


def _validate_publication_receipt(
        receipt: dict, expected_channel: str) -> tuple[bool, str | None]:
    """Validate a publication receipt schema and channel match.

    Returns (is_valid, error_message). Receipt must have:
    - channel (matches expected)
    - status (PUBLISHED, FAILED, NOT_CONFIGURED)
    - remote_id (for PUBLISHED)
    - url (for PUBLISHED)
    - timestamp
    - error (for FAILED/NOT_CONFIGURED)
    """
    if not isinstance(receipt, dict):
        return False, "Receipt must be a dict"
    if receipt.get("channel") != expected_channel:
        return False, f"Receipt channel mismatch: expected {expected_channel}, got {receipt.get('channel')}"
    status = receipt.get("status")
    if status not in {"PUBLISHED", "FAILED", "NOT_CONFIGURED"}:
        return False, f"Invalid receipt status: {status}"
    if not isinstance(receipt.get("timestamp"), (int, float)):
        return False, "Receipt must have numeric timestamp"
    if status == "PUBLISHED":
        if not receipt.get("remote_id"):
            return False, "PUBLISHED receipt must have remote_id"
        # A platform may confirm a post without returning its public address; the
        # adapter then says so explicitly instead of inventing a link.
        if not receipt.get("url") and receipt.get("permalink_available") is not False:
            return False, "PUBLISHED receipt must have url"
        if os.getenv("DEMO_MODE", "false").lower() != "true" and _is_mock_receipt(receipt):
            return False, "Mock publication receipt is not accepted outside demo mode"
    elif status in {"FAILED", "NOT_CONFIGURED"}:
        if not receipt.get("error"):
            return False, f"{status} receipt must have error"
    return True, None


def _redact_payload(value):
    """Recursively redact secret-shaped values in an operator-visible payload.

    Receipts and worker errors cross from an external runtime into CORE state
    that is served by ``/api/jobs`` and rendered in the Web UI.  A secret that a
    publisher or transport put into ``error``/``url``/metadata must be masked at
    this boundary, not only when a log line is written (Issue #190).
    """
    from ...logging_config import redact_payload

    return redact_payload(value)


def _handle_publish_result(
        job_store,
        job,
        result,
        artifacts,
        channel: str) -> None:
    success = result.get("success", False)
    task_id = result.get("task_id")
    if job.active_task_id == task_id:
        job.active_task_id = None

    # Initialize per-channel retry tracking
    if not hasattr(job, 'publish_retry_count'):
        job.publish_retry_count = {}
    if channel not in job.publish_retry_count:
        job.publish_retry_count[channel] = 0

    # Initialize published channels set for idempotency
    if not hasattr(job, 'published_channels'):
        job.published_channels = set()

    def _backoff_seconds(attempt: int) -> float:
        """Bounded exponential backoff for transient channel failures."""
        base = float(os.getenv("PUBLISH_RETRY_BACKOFF_BASE", "2"))
        cap = float(os.getenv("PUBLISH_RETRY_BACKOFF_CAP", "60"))
        return min(cap, base ** max(0, attempt))

    def _enqueue_with_backoff(reason: str) -> None:
        """Retry a failed publish channel with bounded exponential backoff.

        ``max_retries`` counts *additional* attempts after the first failure, so
        ``max_retries=0`` fails immediately and ``max_retries=2`` allows two
        retries (three total attempts).  The counter is incremented only when a
        retry is actually scheduled, so a lost ack does not burn a retry slot.
        """
        # The current task is resolved here: a retry replaces it with a new
        # task_id and an exhausted budget fails the channel for good.  Its id
        # must leave the pending map either way, otherwise the publish stage
        # never sees an empty map and the job cannot reach PUBLISHED/FAILED.
        if task_id and job.publish_task_ids.get(task_id) == channel:
            job.publish_task_ids.pop(task_id, None)
        attempt = job.publish_retry_count.get(channel, 0) + 1
        if attempt <= job.max_retries:
            delay = _backoff_seconds(attempt)
            job.publish_retry_count[channel] = attempt
            job_store.event(
                job, f"PUBLISH {channel}: RETRY {attempt}/{job.max_retries} "
                f"(backoff {delay:.1f}s): {reason}")
            job_store.update(
                job,
                JobStatus.PUBLISHING,
                f"PUBLISH RETRY {attempt}/{job.max_retries}: {channel} ({reason})")
            _enqueue_publish_task(job_store, job, channel, delay=delay)
        else:
            job.publish_retry_count[channel] = attempt
            job.publication_results.setdefault(
                channel, {
                    "channel": channel, "status": "FAILED", "error": f"{reason} AFTER RETRIES"})
            job_store.update(
                job,
                JobStatus.FAILED,
                f"PUBLISH {channel} FAILED: {reason} AFTER RETRIES")

    if success:
        receipt_artifact = next(
            (a for a in artifacts if a.get("kind") == "publication_receipt"), None)
        if not receipt_artifact:
            job_store.event(job, f"PUBLISH {channel}: NO RECEIPT ARTIFACT")
            job.publish_error = "NO RECEIPT ARTIFACT"
            _enqueue_with_backoff("no receipt")
            return

        import base64 as _b64
        try:
            data = _b64.b64decode(receipt_artifact["data_base64"])
            receipt = json.loads(data.decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            detail = secret_redact(str(exc))
            job_store.event(
                job, f"PUBLISH {channel}: MALFORMED RECEIPT: {detail}")
            job.publish_error = f"MALFORMED RECEIPT: {detail}"
            _enqueue_with_backoff(f"malformed receipt: {detail}")
            return

        # Receipts are produced by an external publisher runtime; redact any
        # secret-shaped value before it becomes operator-visible CORE state.
        receipt = _redact_payload(receipt)

        # Validate receipt schema
        valid, error = _validate_publication_receipt(receipt, channel)
        if not valid:
            job_store.event(
                job, f"PUBLISH {channel}: INVALID RECEIPT: {error}")
            job.publish_error = f"INVALID RECEIPT: {error}"
            _enqueue_with_backoff(f"invalid receipt: {error}")
            return

        # Correlate the receipt with the durable publish intent
        # (owner/lease/version).
        decision, reason = _match_publish_receipt(job, channel, receipt)
        if decision == "reject":
            job_store.event(
                job, f"PUBLISH {channel}: RECEIPT REJECTED: {reason}")
            job.publish_error = f"RECEIPT REJECTED: {reason}"
            job.publication_results.setdefault(
                channel, {
                    "channel": channel, "status": "FAILED", "error": f"RECEIPT REJECTED: {reason}"})
            if task_id and job.publish_task_ids.get(task_id) == channel:
                job.publish_task_ids.pop(task_id, None)
            job_store.update(
                job,
                JobStatus.FAILED,
                f"PUBLISH {channel} FAILED: RECEIPT REJECTED")
            return
        if decision == "skip":
            job_store.event(
                job, f"PUBLISH {channel}: ALREADY PUBLISHED (idempotent skip)")
            # Only pop the task_id after successful validation and processing
            if task_id and job.publish_task_ids.get(task_id) == channel:
                job.publish_task_ids.pop(task_id, None)
            return

        receipt_status = receipt.get("status")
        job.publication_results[channel] = receipt

        if receipt_status == "PUBLISHED":
            job.published_channels.add(channel)
            if channel not in job.published_to:
                job.published_to.append(channel)
            job.publish_retry_count[channel] = 0
            job.publish_intent.pop(channel, None)
            # Only pop the task_id after successful validation and processing
            if task_id and job.publish_task_ids.get(task_id) == channel:
                job.publish_task_ids.pop(task_id, None)
            job_store.update(
                job,
                JobStatus.PUBLISHING,
                f"PUBLISH TASK {task_id} COMPLETED FOR {channel}")
        elif receipt_status == "NOT_CONFIGURED":
            job.publish_error = receipt.get("error", "NOT_CONFIGURED")
            job.publish_retry_count[channel] = 0
            job.publish_intent.pop(channel, None)
            # Only pop the task_id after successful validation and processing
            if task_id and job.publish_task_ids.get(task_id) == channel:
                job.publish_task_ids.pop(task_id, None)
            job_store.event(job, f"PUBLISH {channel} NOT CONFIGURED; NO RETRY")
            return
        else:  # FAILED — permanent or transient; retry with bounded backoff
            job.publish_error = receipt.get("error", "UNKNOWN")
            # Only pop the task_id after successful validation and processing
            if task_id and job.publish_task_ids.get(task_id) == channel:
                job.publish_task_ids.pop(task_id, None)
            if receipt.get("retryable") is False:
                if not receipt.get("outcome_unknown"):
                    job.publish_intent.pop(channel, None)
                job_store.event(
                    job, f"PUBLISH {channel} PERMANENT FAILURE; NO RETRY")
                return
            _enqueue_with_backoff(f"FAILED: {job.publish_error}")
    else:
        error = secret_redact(
            str(result.get("error") or "UNKNOWN PUBLISH ERROR"))
        job.publish_error = error
        job_store.event(job, f"PUBLISH TASK FAILED for {channel}: {error}")
        _enqueue_with_backoff(error)
