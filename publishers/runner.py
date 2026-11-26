"""Publication of one approved video through the platform adapters (#510/#511).

Extracted unchanged from ``worker.role_executor.execute_publisher`` so the same code can run
in the Worker (today) and in the separately released Publisher module container (next
stages). The node-specific parts are passed in: how an artifact is built, where CORE is,
how an approved input is fetched and how task-scoped credentials are provided.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Callable


def run_publication(task: dict, *, artifact: Callable[[str, str, bytes], dict],
                    core_base_url: Callable[[], str],
                    fetch_input: Callable[[dict, str, Path, str], Path],
                    secret_source: Callable[[dict], object],
                    use_task_secrets: Callable[[object], object],
                    upload: Callable[[object, str, str, dict], dict] | None = None) -> list[dict]:
    """Publish a video to a platform via the Publisher Worker.

    Runs the platform adapter directly (YoutubePublisher, TikTokPublisher, etc.)
    using ``PUBLISHER_MOCK`` for sandbox mode.  Returns a ``publication_receipt``
    artifact carrying platform, remote_id, url, timestamp, status/error.

    Transient failures (network errors, rate limits) raise exceptions so the
    worker marks the task as failed and CORE retries with backoff.
    Permanent failures (NOT_CONFIGURED) return a receipt with status=NOT_CONFIGURED
    and the worker marks success=True; CORE will not retry these.
    """
    import time as _time

    channel = task["channel"]
    video_path = task.get("video_path", "")
    metadata = task.get("metadata", {})
    delivery_contract = task.get("delivery_contract") or {}
    publish_intent = task.get("publish_intent") or {}
    ledger_path = None
    identity = {"job_id": task["job_id"], "channel": channel,
                "video_version": publish_intent.get("video_version"),
                "video_sha256": publish_intent.get("video_sha256")}
    if task.get("task_id") and identity["video_version"] is not None and identity["video_sha256"]:
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        ledger_path = Path(os.getenv("JOB_ROOT", "/data/jobs")) / task["job_id"] / "publication-ledger" / f"{key}.json"
        if ledger_path.is_file():
            previous = json.loads(ledger_path.read_text(encoding="utf-8"))
            if previous.get("intent") != identity:
                raise RuntimeError("Publication ledger intent mismatch")
            if previous.get("state") == "UPLOADING":
                raise RuntimeError("Unknown upload outcome; reconciliation required before another upload")
            cached_receipt = previous.get("receipt")
        else:
            cached_receipt = None
    else:
        cached_receipt = None
    def save_ledger(state, receipt=None):
        if ledger_path is None:
            return
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = ledger_path.with_name(f".{ledger_path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"intent": identity, "state": state, "receipt": receipt}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, ledger_path)
    def receiptartifact(receipt):
        receipt.update({"job_id": task["job_id"], "task_id": task.get("task_id"),
                        "channel": channel,
                        "video_version": publish_intent.get("video_version"),
                        "video_sha256": publish_intent.get("video_sha256")})
        return [artifact("publication.json", "publication_receipt",
                          json.dumps(receipt, sort_keys=True).encode("utf-8"))]
    if cached_receipt and cached_receipt.get("status") == "PUBLISHED":
        return receiptartifact(cached_receipt)
    from publishers.base import publisher_mock_enabled
    if publisher_mock_enabled():
        receipt = {
            "channel": channel, "status": "PUBLISHED",
            "id": f"mock-{uuid.uuid4().hex[:12]}",
            "remote_id": f"mock-{uuid.uuid4().hex[:12]}",
            "url": f"https://example.invalid/{channel}/mock-{uuid.uuid4().hex[:8]}",
            "timestamp": _time.time(),
            "video_version": publish_intent.get("video_version"),
            "video_sha256": publish_intent.get("video_sha256"),
            "upload": {"mode": "mock", "bytes": os.path.getsize(video_path) if video_path and os.path.isfile(video_path) else 0},
        }
        return receiptartifact(receipt)
    from publishers import LIVE_PUBLISHERS
    publisher = LIVE_PUBLISHERS.get(channel)
    # The node may have no DATABASE_URL/env tokens: credentials come from CORE per task.
    secrets = secret_source(task)
    with use_task_secrets(secrets):
        configured = publisher is not None and publisher.configured()
    if not configured:
        receipt = {"channel": channel, "status": "NOT_CONFIGURED",
                   "error": f"{publisher.credential_env if publisher else 'unknown'} is missing",
                   "timestamp": _time.time()}
        return receiptartifact(receipt)
    # Issue #199/#166.4: verify the approved artifact against the delivery
    # contract before any upload.  A missing/corrupt/mismatched file must not
    # reach the adapter.  If the file is not available locally (disposable
    # Publisher with separate filesystem), fetch it from CORE.
    try:
        expected_sha = (delivery_contract.get("sha256") or delivery_contract.get("video_sha256"))
        expected_size = delivery_contract.get("size_bytes") or delivery_contract.get("size")
        if task.get("task_id") and publish_intent:
            expected_version = delivery_contract.get("version", delivery_contract.get("video_version"))
            if (not expected_sha or expected_size is None or expected_version is None
                    or str(expected_version) != str(publish_intent.get("video_version"))
                    or expected_sha != publish_intent.get("video_sha256")
                    or publish_intent.get("job_id") != task["job_id"]
                    or publish_intent.get("channel") != channel):
                raise ValueError("delivery contract does not match issued publish intent")
        if task.get("task_id") and delivery_contract and core_base_url():
            # A Publisher host never writes to CORE's absolute filesystem path.
            # Its disposable copy is authenticated and checked before upload.
            source_name = Path(video_path).name
            video_path = str(Path(os.getenv("JOB_ROOT", "/data/jobs")) / task["job_id"] / "publish-inputs" / source_name)
            if not os.path.isfile(video_path):
                fetch_input(task, source_name, Path(video_path), str(expected_sha or ""))
        if video_path and not os.path.isfile(video_path):
            # Fetch approved video from CORE for disposable Publisher node
            from pathlib import Path as _Path
            video_path_obj = _Path(video_path)
            video_path_obj.parent.mkdir(parents=True, exist_ok=True)
            fetch_input(task, video_path_obj.name, video_path_obj, str(expected_sha) if expected_sha else "")
        if video_path and os.path.isfile(video_path):
            if expected_sha:
                import hashlib as _hashlib
                actual_sha = _hashlib.sha256(open(video_path, "rb").read()).hexdigest()
                if actual_sha != str(expected_sha).removeprefix("sha256:"):
                    raise ValueError("delivery contract sha256 mismatch")
            if expected_size is not None and os.path.getsize(video_path) != int(expected_size):
                raise ValueError("delivery contract size mismatch")
    except ValueError as error:
        receipt = {"channel": channel, "status": "FAILED", "error": str(error),
                   "timestamp": _time.time()}
        return receiptartifact(receipt)
    except (OSError, RuntimeError) as error:
        receipt = {"channel": channel, "status": "FAILED", "error": str(error),
                   "timestamp": _time.time()}
        return receiptartifact(receipt)
    try:
        save_ledger("UPLOADING")
        with use_task_secrets(secrets):
            # Stage 3 (#510/#511): the upload may run in the Publisher module container.
            result = (upload(publisher, channel, video_path, metadata) if upload is not None
                      else publisher.publish(video_path, metadata))
    except Exception as error:
        # Transient failures (network, rate limit) should raise so worker retries
        # Permanent failures should be caught by publisher and returned in result
        error_str = str(error).lower()
        if any(keyword in error_str for keyword in ("timeout", "network", "connection", "rate limit", "429", "503", "504")):
            raise  # Transient - let worker retry
        # Other exceptions treated as permanent failure
        from core.logging_config import secret_redact
        result = {"channel": channel, "status": "FAILED", "error": secret_redact(str(error))}
    if isinstance(result, dict) and "error" in result:
        from core.logging_config import secret_redact
        result["error"] = secret_redact(result["error"])
    result.setdefault("channel", channel)
    result.setdefault("timestamp", _time.time())
    # Correlate the receipt back to the durable delivery contract so CORE can
    # match it to the publish intent (owner/lease/version) on the next attempt.
    if publish_intent.get("video_version") is not None:
        result.setdefault("video_version", publish_intent["video_version"])
    if publish_intent.get("video_sha256"):
        result.setdefault("video_sha256", publish_intent["video_sha256"])
    result.setdefault("remote_id", result.get("id"))
    save_ledger("UPLOADING" if result.get("outcome_unknown") else
                "PUBLISHED" if result.get("status") == "PUBLISHED" else "FAILED", result)
    return receiptartifact(result)


