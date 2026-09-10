"""Backfill: migrate file-based jobs/channels into PostgreSQL once.

Runs only when STORAGE_BACKEND=postgres. Copies any jobs found in the legacy
file location (JOB_ROOT / legacy JOB_ROOT fallbacks) into the `jobs` table
without overwriting existing rows. Idempotent and resumable.
"""
from __future__ import annotations
import json, os
from pathlib import Path

BATCH = 50

def _file_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("JOB_ROOT",):
        v = os.getenv(key)
        if v:
            roots.append(Path(v))
    # legacy defaults prior to persistent move
    roots.extend([Path("/data/storage/jobs"), Path("/data/jobs"), Path("jobs")])
    seen: set[str] = set()
    out: list[Path] = []
    for p in roots:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out

def run_batch(connection, checkpoint: dict) -> dict:
    # Already done
    if checkpoint.get("done"):
        return {"done": True, "checkpoint": checkpoint}
    # Resume position
    offset = int(checkpoint.get("offset", 0))
    # Collect file jobs
    files: list[Path] = []
    for root in _file_roots():
        files.extend(sorted(root.glob("*/job.json")))
    # Deduplicate by path string
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in files:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    slice_ = uniq[offset: offset + BATCH]
    imported = 0
    for path in slice_:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            job_id = raw.get("job_id")
            if not job_id:
                continue
            # Skip if already present
            row = connection.execute("SELECT 1 FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
            if row:
                continue
            # Insert minimal job row; PostgresRepository save path will keep payload.
            # We reuse the full JSON payload as stored in the file (it is already a Job dump).
            # Use available fields with safe defaults.
            from datetime import datetime, timezone
            payload_text = json.dumps(raw)
            # Extract fields used in current schema; fall back to payload content.
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
            # Telegram / events / tasks history are not backfilled; not critical.
            imported += 1
        except Exception:
            # Ignore individual file errors, continue.
            continue
    next_offset = offset + len(slice_)
    done = next_offset >= len(uniq)
    return {"done": bool(done), "checkpoint": {"offset": next_offset, "imported": int(checkpoint.get("imported", 0)) + imported, "done": bool(done)}}
