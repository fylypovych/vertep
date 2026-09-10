"""Backfill: migrate file-based jobs/channels into PostgreSQL once.

Runs only when STORAGE_BACKEND=postgres. Copies any jobs found in the legacy
file location (JOB_ROOT / legacy JOB_ROOT fallbacks) into the `jobs` table
without overwriting existing rows. Idempotent and resumable.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

BATCH = 50

def _file_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("JOB_ROOT",):
        v = os.getenv(key)
        if v:
            roots.append(Path(v))
    roots.extend([Path("/data/storage/jobs"), Path("/data/jobs"), Path("jobs")])
    seen: set[str] = set()
    out: list[Path] = []
    for p in roots:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _load_job_from_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _collect_file_jobs() -> list[Path]:
    files: list[Path] = []
    for root in _file_roots():
        files.extend(sorted(root.glob("*/job.json")))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in files:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _migrate_job(connection, job_id: str, raw: dict) -> bool:
    row = connection.execute("SELECT 1 FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
    if row:
        return False

    from datetime import datetime, timezone
    payload_text = json.dumps(raw)
    topic = raw.get("topic") or raw.get("payload", {}).get("topic") or ""
    char_id = raw.get("character_id") or raw.get("payload", {}).get("character_id") or "unknown"
    status = raw.get("status") or "NEW"
    priority = int(raw.get("priority", 5))
    source = raw.get("source") or "web"
    created_at = raw.get("created_at") or datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT INTO jobs(job_id,topic,character_id,status,priority,source,created_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO NOTHING",
        (job_id, topic, char_id, status, priority, source, created_at, payload_text),
    )
    return True


def _migrate_events(connection, job_id: str, job_root: Path) -> int:
    events = _load_jsonl(job_root / "events.jsonl")
    if not events:
        return 0
    count = 0
    for ev in events:
        created_at = ev.get("created_at")
        message = ev.get("message")
        if not created_at or not message:
            continue
        connection.execute(
            "INSERT INTO job_events(job_id,created_at,message) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (job_id, created_at, message),
        )
        count += 1
    return count


def _migrate_tasks(connection, job_root: Path) -> int:
    tasks = _load_jsonl(job_root / ".tasks.jsonl")
    if not tasks:
        return 0
    count = 0
    for entry in tasks:
        task = entry.get("task", {})
        task_id = task.get("task_id")
        job_id = task.get("job_id")
        if not task_id or not job_id:
            continue
        node_name = entry.get("node_name")
        status = entry.get("status", "PENDING")
        error = entry.get("error")
        task_type = task.get("task", "image")
        connection.execute(
            """INSERT INTO task_attempts(task_id,job_id,node_name,task_type,status,error)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(task_id) DO UPDATE SET
            node_name=excluded.node_name,status=excluded.status,error=excluded.error,
            completed_at=CASE WHEN excluded.status IN ('COMPLETED','FAILED','CANCELLED') THEN now() END""",
            (task_id, job_id, node_name, task_type, status, error),
        )
        count += 1
    return count


def _migrate_scenes(connection, job_id: str, raw: dict) -> int:
    scenes = raw.get("scenes", [])
    if not scenes:
        return 0
    count = 0
    for scene in scenes:
        scene_id = scene.get("scene_id")
        if not scene_id:
            continue
        scene_index = scene.get("index", 0)
        status = scene.get("status", "PENDING")
        payload = json.dumps(scene)
        connection.execute(
            """INSERT INTO scenes(job_id,scene_id,scene_index,status,payload)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job_id,scene_id) DO UPDATE SET
            scene_index=excluded.scene_index,status=excluded.status,payload=excluded.payload""",
            (job_id, scene_id, scene_index, status, payload),
        )
        count += 1
    return count


def _migrate_artifacts(connection, job_id: str, raw: dict) -> int:
    artifacts = raw.get("artifacts", [])
    if not artifacts:
        return 0
    count = 0
    for art in artifacts:
        artifact_id = art.get("artifact_id")
        if not artifact_id:
            continue
        scene_id = art.get("scene_id")
        kind = art.get("kind", "image")
        path = art.get("path", "")
        mime_type = art.get("mime_type", "image/png")
        size = int(art.get("size", 0))
        sha256 = art.get("sha256", "")
        provenance = {"task_id": art.get("task_id"), "node_name": art.get("node_name"), "workflow": art.get("workflow")}
        created_at = art.get("created_at")
        connection.execute(
            """INSERT INTO artifacts(artifact_id,job_id,scene_id,kind,path,mime_type,size,sha256,provenance,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(artifact_id) DO UPDATE SET
            job_id=excluded.job_id,scene_id=excluded.scene_id,kind=excluded.kind,path=excluded.path,
            mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,provenance=excluded.provenance,created_at=excluded.created_at""",
            (artifact_id, job_id, scene_id, kind, path, mime_type, size, sha256, json.dumps(provenance), created_at),
        )
        count += 1
    return count


def _migrate_stage_attempts(connection, job_id: str, raw: dict) -> int:
    stages = raw.get("stages", {})
    if not stages:
        return 0
    count = 0
    for stage_name, stage in stages.items():
        attempts = stage.get("attempts", [])
        for attempt in attempts:
            attempt_num = attempt.get("attempt", 1)
            status = attempt.get("status", "PENDING")
            payload = json.dumps(attempt)
            connection.execute(
                """INSERT INTO stage_attempts(job_id,stage_name,attempt,status,payload)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job_id,stage_name,attempt) DO UPDATE SET
                status=excluded.status,payload=excluded.payload""",
                (job_id, stage_name, attempt_num, status, payload),
            )
            count += 1
    return count


def _migrate_publications(connection, job_id: str, raw: dict) -> int:
    pubs = raw.get("publication_results", {})
    if not pubs:
        return 0
    count = 0
    for channel, result in pubs.items():
        status = result.get("status", "UNKNOWN")
        external_id = result.get("external_id")
        external_url = result.get("external_url")
        payload = json.dumps(result)
        connection.execute(
            """INSERT INTO publications(job_id,channel,status,external_id,external_url,payload)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,channel) DO UPDATE SET
            status=excluded.status,external_id=excluded.external_id,external_url=excluded.external_url,payload=excluded.payload""",
            (job_id, channel, status, external_id, external_url, payload),
        )
        count += 1
    return count


def _migrate_telegram_updates(connection, job_root: Path) -> int:
    updates = _load_json(job_root / ".telegram_updates.json")
    if not updates:
        return 0
    count = 0
    for key, payload in updates.items():
        if ":" not in key:
            continue
        chat_id, message_id = key.split(":", 1)
        connection.execute(
            "INSERT INTO telegram_updates(chat_id,message_id,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (chat_id, message_id, json.dumps(payload)),
        )
        count += 1
    return count


def _migrate_workers(connection, root: Path) -> int:
    worker_file = root / ".workers.json"
    workers = _load_json(worker_file)
    if not workers:
        return 0
    count = 0
    for worker in workers.values():
        node_name = worker.get("node_name")
        if not node_name:
            continue
        status = worker.get("status", "OFFLINE")
        last_seen = worker.get("last_seen")
        capabilities = json.dumps(worker)
        connection.execute(
            """INSERT INTO workers(node_name,status,last_seen,capabilities) VALUES(%s,%s,%s,%s)
            ON CONFLICT(node_name) DO UPDATE SET status=excluded.status,last_seen=excluded.last_seen,capabilities=excluded.capabilities""",
            (node_name, status, last_seen, capabilities),
        )
        count += 1
    return count


def _migrate_channels(connection, root: Path) -> int:
    channel_file = root / ".channels.json"
    channels = _load_json(channel_file)
    if not channels:
        return 0
    count = 0
    for channel in channels.values():
        channel_id = channel.get("channel_id")
        if not channel_id:
            continue
        brand_id = channel.get("brand_id", "brand01")
        channel_type = channel.get("channel_type", "telegram")
        target = channel.get("target", "")
        enabled = channel.get("enabled", True)
        created_at = channel.get("created_at")
        metadata = json.dumps(channel.get("metadata", {}))
        connection.execute(
            """INSERT INTO channels(channel_id,brand_id,channel_type,target,enabled,created_at,metadata)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(channel_id) DO UPDATE SET
            target=excluded.target,enabled=excluded.enabled,metadata=excluded.metadata""",
            (channel_id, brand_id, channel_type, target, enabled, created_at, metadata),
        )
        count += 1
    return count


def _update_job_sequence(connection, year: int, sequence: int) -> None:
    connection.execute(
        """INSERT INTO job_sequences(year,next_value) VALUES(%s,%s)
        ON CONFLICT(year) DO UPDATE SET next_value=GREATEST(job_sequences.next_value, excluded.next_value)""",
        (year, sequence),
    )


def run_batch(connection, checkpoint: dict) -> dict:
    if checkpoint.get("done"):
        return {"done": True, "checkpoint": checkpoint}

    offset = int(checkpoint.get("offset", 0))
    phase = checkpoint.get("phase", "jobs")

    files = _collect_file_jobs()
    total = len(files)

    if phase == "jobs":
        slice_ = files[offset: offset + BATCH]
        imported = 0
        max_seq = checkpoint.get("max_sequence", 0)
        for path in slice_:
            try:
                raw = _load_job_from_file(path)
                if not raw:
                    continue
                job_id = raw.get("job_id")
                if not job_id:
                    continue
                if _migrate_job(connection, job_id, raw):
                    imported += 1
                    parts = job_id.split("-")
                    if len(parts) == 2 and parts[0].isdigit():
                        yr = int(parts[0])
                        seq = int(parts[1])
                        if seq > max_seq:
                            max_seq = seq
                            _update_job_sequence(connection, yr, seq)
            except Exception:
                continue

        next_offset = offset + len(slice_)
        done = next_offset >= total
        if done:
            return {"done": False, "checkpoint": {"offset": 0, "imported": checkpoint.get("imported", 0) + imported, "max_sequence": max_seq, "phase": "events"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "imported": checkpoint.get("imported", 0) + imported, "max_sequence": max_seq, "phase": "jobs"}}

    if phase == "events":
        done = 0
        for path in files[offset: offset + BATCH]:
            job_id = path.parent.name
            _migrate_events(connection, job_id, path.parent)
            done += 1
        next_offset = offset + BATCH
        if next_offset >= total:
            return {"done": False, "checkpoint": {"offset": 0, "phase": "tasks"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "events"}}

    if phase == "tasks":
        roots = _file_roots()
        task_files = []
        for root in roots:
            task_files.append(root / ".tasks.jsonl")
        slice_ = task_files[offset: offset + BATCH]
        for tf in slice_:
            _migrate_tasks(connection, tf.parent)
        next_offset = offset + len(slice_)
        if next_offset >= len(task_files):
            return {"done": False, "checkpoint": {"offset": 0, "phase": "scenes"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "tasks"}}

    if phase == "scenes":
        for path in files[offset: offset + BATCH]:
            raw = _load_job_from_file(path)
            if raw:
                _migrate_scenes(connection, path.parent.name, raw)
        next_offset = offset + BATCH
        if next_offset >= total:
            return {"done": False, "checkpoint": {"offset": 0, "phase": "artifacts"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "scenes"}}

    if phase == "artifacts":
        for path in files[offset: offset + BATCH]:
            raw = _load_job_from_file(path)
            if raw:
                _migrate_artifacts(connection, path.parent.name, raw)
        next_offset = offset + BATCH
        if next_offset >= total:
            return {"done": False, "checkpoint": {"offset": 0, "phase": "stage_attempts"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "artifacts"}}

    if phase == "stage_attempts":
        for path in files[offset: offset + BATCH]:
            raw = _load_job_from_file(path)
            if raw:
                _migrate_stage_attempts(connection, path.parent.name, raw)
        next_offset = offset + BATCH
        if next_offset >= total:
            return {"done": False, "checkpoint": {"offset": 0, "phase": "publications"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "stage_attempts"}}

    if phase == "publications":
        for path in files[offset: offset + BATCH]:
            raw = _load_job_from_file(path)
            if raw:
                _migrate_publications(connection, path.parent.name, raw)
        next_offset = offset + BATCH
        if next_offset >= total:
            return {"done": False, "checkpoint": {"offset": 0, "phase": "telegram"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "publications"}}

    if phase == "telegram":
        for path in files[offset: offset + BATCH]:
            _migrate_telegram_updates(connection, path.parent)
        next_offset = offset + BATCH
        if next_offset >= total:
            return {"done": False, "checkpoint": {"offset": 0, "phase": "workers"}}
        return {"done": False, "checkpoint": {"offset": next_offset, "phase": "telegram"}}

    if phase == "workers":
        roots = _file_roots()
        for root in roots:
            _migrate_workers(connection, root)
        return {"done": False, "checkpoint": {"offset": 0, "phase": "channels"}}

    if phase == "channels":
        roots = _file_roots()
        for root in roots:
            _migrate_channels(connection, root)
        return {"done": True, "checkpoint": {"done": True, "imported": checkpoint.get("imported", 0)}}

    return {"done": True, "checkpoint": {"done": True}}