"""Durable workflow documents in configured Redis or PostgreSQL storage."""

from copy import deepcopy
from datetime import datetime, timezone
import json

from .state import WorkflowState


class DatabaseWorkflowState(WorkflowState):
    def __init__(self, storage_backend: str, url: str):
        self.storage_backend = storage_backend
        if storage_backend == "postgres":
            import psycopg
            self._connect = lambda: psycopg.connect(url)
            with self._connect() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS workflow_documents (job_id TEXT PRIMARY KEY, document JSONB NOT NULL)")
        elif storage_backend == "redis":
            import redis
            self.redis = redis.Redis.from_url(url, decode_responses=True)
            self.redis.ping()
        else:
            raise ValueError("Unsupported workflow database backend")

    def save_job(self, job_id, state):
        document = deepcopy(state)
        document["saved_at"] = datetime.now(timezone.utc).isoformat()
        raw = json.dumps(document, ensure_ascii=False, default=str)
        if self.storage_backend == "postgres":
            with self._connect() as connection:
                connection.execute("INSERT INTO workflow_documents(job_id,document) VALUES(%s,%s::jsonb) ON CONFLICT(job_id) DO UPDATE SET document=excluded.document", (job_id, raw))
        else:
            # SET stores job/tasks/outputs/approval as one atomic document.
            self.redis.set(f"vertep:workflow:{job_id}", raw)

    def load_job(self, job_id):
        if self.storage_backend == "postgres":
            with self._connect() as connection:
                row = connection.execute("SELECT document FROM workflow_documents WHERE job_id=%s", (job_id,)).fetchone()
            return row[0] if row else None
        raw = self.redis.get(f"vertep:workflow:{job_id}")
        return json.loads(raw) if raw else None

    def list_checkpoints(self):
        if self.storage_backend == "postgres":
            with self._connect() as connection:
                return [row[0] for row in connection.execute("SELECT job_id FROM workflow_documents").fetchall()]
        prefix = "vertep:workflow:"
        return [key[len(prefix):] for key in self.redis.scan_iter(match=prefix + "*")]

    def restore_from_checkpoint(self, checkpoint):
        if not checkpoint.get("job_id") or not isinstance(checkpoint.get("state"), dict):
            return False
        self.save_job(checkpoint["job_id"], checkpoint["state"])
        return True
