import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from .models import Job, Channel


class StateRepository(ABC):
    @abstractmethod
    def next_job_sequence(self, year: int, current: int) -> int: ...

    @abstractmethod
    def load_jobs(self) -> Iterable[Job]: ...

    @abstractmethod
    def save_job(self, job: Job) -> None: ...

    @abstractmethod
    def delete_job(self, job_id: str) -> None: ...

    @abstractmethod
    def save_worker(self, worker: dict) -> None: ...

    @abstractmethod
    def load_workers(self, role: str | None = None, status: str | None = None, capability: str | None = None) -> Iterable[dict]: ...

    @abstractmethod
    def append_event(self, job_id: str, created_at: str, message: str) -> None: ...

    @abstractmethod
    def record_task(self, task: dict, status: str, node_name: str | None = None, error: str | None = None) -> None: ...

    @abstractmethod
    def has_telegram_update(self, chat_id: str, message_id: str) -> bool: ...

    @abstractmethod
    def record_telegram_update(self, chat_id: str, message_id: str, payload: dict) -> None: ...

    def list_channels(self, brand_id: str | None = None) -> list[Channel]:
        return []

    def get_channel(self, channel_id: str) -> Channel | None:
        return None

    def save_channel(self, channel: Channel) -> None:
        pass

    def delete_channel(self, channel_id: str) -> None:
        pass


class FileRepository(StateRepository):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.worker_file = self.root / ".workers.json"

    def load_jobs(self) -> Iterable[Job]:
        for path in self.root.glob("*/job.json"):
            try:
                yield Job.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

    def next_job_sequence(self, year: int, current: int) -> int:
        return current + 1

    def save_job(self, job: Job) -> None:
        directory = self.root / job.job_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "job.json.tmp"
        temporary.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(directory / "job.json")

    def delete_job(self, job_id: str) -> None:
        # JobStore owns removal of the complete artifact directory.
        return None

    def save_worker(self, worker: dict) -> None:
        try:
            current = json.loads(self.worker_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        current[worker["node_name"]] = worker
        temporary = self.worker_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.worker_file)

    def load_workers(self, role: str | None = None, status: str | None = None, capability: str | None = None) -> Iterable[dict]:
        try:
            workers = list(json.loads(self.worker_file.read_text(encoding="utf-8")).values())
        except (OSError, ValueError):
            workers = []
        if role:
            workers = [worker for worker in workers if worker.get("role") == role]
        if status:
            workers = [worker for worker in workers if worker.get("status") == status]
        if capability:
            workers = [worker for worker in workers if capability in (worker.get("capabilities") or [])]
        return workers

    def append_event(self, job_id: str, created_at: str, message: str) -> None:
        path = self.root / job_id / "events.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"created_at": created_at, "message": message}, ensure_ascii=False) + "\n")

    def record_task(self, task: dict, status: str, node_name: str | None = None, error: str | None = None) -> None:
        with (self.root / ".tasks.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"task": task, "status": status, "node_name": node_name, "error": error}, ensure_ascii=False) + "\n")

    def has_telegram_update(self, chat_id: str, message_id: str) -> bool:
        path = self.root / ".telegram_updates.json"
        try:
            return f"{chat_id}:{message_id}" in json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False

    def record_telegram_update(self, chat_id: str, message_id: str, payload: dict) -> None:
        path = self.root / ".telegram_updates.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        current[f"{chat_id}:{message_id}"] = payload
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


class MemoryRepository(StateRepository):
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.workers: dict[str, dict] = {}
        self.events: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.telegram_updates: dict[str, dict] = {}

    def load_jobs(self) -> Iterable[Job]:
        return list(self.jobs.values())

    def next_job_sequence(self, year: int, current: int) -> int:
        return current + 1

    def save_job(self, job: Job) -> None:
        self.jobs[job.job_id] = job.model_copy(deep=True)

    def delete_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    def save_worker(self, worker: dict) -> None:
        self.workers[worker["node_name"]] = dict(worker)

    def load_workers(self, role: str | None = None, status: str | None = None, capability: str | None = None) -> Iterable[dict]:
        workers = list(self.workers.values())
        if role:
            workers = [worker for worker in workers if worker.get("role") == role]
        if status:
            workers = [worker for worker in workers if worker.get("status") == status]
        if capability:
            workers = [worker for worker in workers if capability in (worker.get("capabilities") or [])]
        return workers

    def append_event(self, job_id: str, created_at: str, message: str) -> None:
        self.events.append({"job_id": job_id, "created_at": created_at, "message": message})

    def record_task(self, task: dict, status: str, node_name: str | None = None, error: str | None = None) -> None:
        self.tasks[task["task_id"]] = {"task": dict(task), "status": status, "node_name": node_name, "error": error}

    def has_telegram_update(self, chat_id: str, message_id: str) -> bool:
        return f"{chat_id}:{message_id}" in self.telegram_updates

    def record_telegram_update(self, chat_id: str, message_id: str, payload: dict) -> None:
        self.telegram_updates[f"{chat_id}:{message_id}"] = dict(payload)


class PostgresRepository(StateRepository):
    """Optional backend. Importing the project never requires psycopg."""

    def __init__(self, dsn: str):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("PostgreSQL backend requires the optional psycopg package") from error
        self.psycopg = psycopg
        self.dsn = dsn

    def load_jobs(self) -> Iterable[Job]:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM jobs ORDER BY created_at")
            return [Job.model_validate(row[0]) for row in cursor.fetchall()]

    def next_job_sequence(self, year: int, current: int) -> int:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO job_sequences(year,next_value) VALUES(%s,1)
                ON CONFLICT(year) DO UPDATE SET next_value=job_sequences.next_value+1 RETURNING next_value""", (year,))
            return int(cursor.fetchone()[0])

    def save_job(self, job: Job) -> None:
        payload = job.model_dump(mode="json")
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO jobs(job_id,topic,character_id,status,priority,source,created_at,payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,priority=excluded.priority,payload=excluded.payload""",
                (job.job_id, job.topic, job.character_id, job.status.value, job.priority, job.source,
                 job.created_at, json.dumps(payload)))
            cursor.execute("DELETE FROM scenes WHERE job_id=%s", (job.job_id,))
            for scene in job.scenes:
                cursor.execute("""INSERT INTO scenes(job_id,scene_id,scene_index,status,payload)
                    VALUES(%s,%s,%s,%s,%s)""", (job.job_id, scene.scene_id, scene.index,
                                                  scene.status.value, json.dumps(scene.model_dump(mode="json"))))
            cursor.execute("DELETE FROM artifacts WHERE job_id=%s", (job.job_id,))
            for artifact in job.artifacts:
                provenance = {"task_id": artifact.task_id, "node_name": artifact.node_name,
                              "workflow": artifact.workflow}
                cursor.execute("""INSERT INTO artifacts(artifact_id,job_id,scene_id,kind,path,mime_type,size,
                    sha256,provenance,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (artifact.artifact_id, job.job_id, artifact.scene_id, artifact.kind, artifact.path,
                     artifact.mime_type, artifact.size, artifact.sha256, json.dumps(provenance),
                     artifact.created_at))
            cursor.execute("DELETE FROM stage_attempts WHERE job_id=%s", (job.job_id,))
            for stage_name, stage in job.stages.items():
                for attempt in stage.attempts:
                    cursor.execute("""INSERT INTO stage_attempts(job_id,stage_name,attempt,status,payload)
                        VALUES(%s,%s,%s,%s,%s)""", (job.job_id, stage_name, attempt.attempt,
                                                     attempt.status, json.dumps(attempt.model_dump(mode="json"))))

    def delete_job(self, job_id: str) -> None:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM jobs WHERE job_id=%s", (job_id,))

    def save_worker(self, worker: dict) -> None:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO workers(node_name,status,last_seen,capabilities) VALUES(%s,%s,%s,%s)
                ON CONFLICT(node_name) DO UPDATE SET status=excluded.status,last_seen=excluded.last_seen,
                capabilities=excluded.capabilities""", (worker["node_name"], worker["status"], worker["last_seen"],
                                                         json.dumps(worker)))

    def load_workers(self, role: str | None = None, status: str | None = None, capability: str | None = None) -> Iterable[dict]:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT capabilities FROM workers")
            workers = [row[0] for row in cursor.fetchall()]
            if role:
                workers = [worker for worker in workers if worker.get("role") == role]
            if status:
                workers = [worker for worker in workers if worker.get("status") == status]
            if capability:
                workers = [worker for worker in workers if capability in (worker.get("capabilities") or [])]
            return workers

    def append_event(self, job_id: str, created_at: str, message: str) -> None:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO job_events(job_id,created_at,message) VALUES(%s,%s,%s)", (job_id, created_at, message))

    def record_task(self, task: dict, status: str, node_name: str | None = None, error: str | None = None) -> None:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO task_attempts(task_id,job_id,node_name,task_type,status,error)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(task_id) DO UPDATE SET node_name=excluded.node_name,
                status=excluded.status,error=excluded.error,completed_at=CASE WHEN excluded.status IN ('COMPLETED','FAILED','CANCELLED') THEN now() END""",
                (task["task_id"], task["job_id"], node_name, task.get("task", "image"), status, error))

    def has_telegram_update(self, chat_id: str, message_id: str) -> bool:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM telegram_updates WHERE chat_id=%s AND message_id=%s", (chat_id, message_id))
            return cursor.fetchone() is not None

    def record_telegram_update(self, chat_id: str, message_id: str, payload: dict) -> None:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO telegram_updates(chat_id,message_id,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                           (chat_id, message_id, json.dumps(payload)))

    def _ensure_channels_table(self) -> None:
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                brand_id TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                target TEXT NOT NULL,
                enabled BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                metadata JSONB DEFAULT '{}')""")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_channels_brand ON channels(brand_id)")

    def list_channels(self, brand_id: str | None = None) -> list[Channel]:
        self._ensure_channels_table()
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            if brand_id:
                cursor.execute("SELECT channel_id, brand_id, channel_type, target, enabled, created_at, metadata FROM channels WHERE brand_id=%s ORDER BY created_at", (brand_id,))
            else:
                cursor.execute("SELECT channel_id, brand_id, channel_type, target, enabled, created_at, metadata FROM channels ORDER BY created_at")
            return [Channel(channel_id=row[0], brand_id=row[1], channel_type=row[2], target=row[3],
                            enabled=row[4], created_at=row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5]),
                            metadata=row[6] if isinstance(row[6], dict) else json.loads(row[6]) if row[6] else {})
                    for row in cursor.fetchall()]

    def get_channel(self, channel_id: str) -> Channel | None:
        self._ensure_channels_table()
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT channel_id, brand_id, channel_type, target, enabled, created_at, metadata FROM channels WHERE channel_id=%s", (channel_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return Channel(channel_id=row[0], brand_id=row[1], channel_type=row[2], target=row[3],
                           enabled=row[4], created_at=row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5]),
                           metadata=row[6] if isinstance(row[6], dict) else json.loads(row[6]) if row[6] else {})

    def save_channel(self, channel: Channel) -> None:
        self._ensure_channels_table()
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO channels(channel_id, brand_id, channel_type, target, enabled, created_at, metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(channel_id) DO UPDATE SET
                target=excluded.target, enabled=excluded.enabled, metadata=excluded.metadata""",
                (channel.channel_id, channel.brand_id, channel.channel_type, channel.target,
                 channel.enabled, channel.created_at, json.dumps(channel.metadata)))

    def delete_channel(self, channel_id: str) -> None:
        self._ensure_channels_table()
        with self.psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM channels WHERE channel_id=%s", (channel_id,))


def build_repository(root: str | Path) -> StateRepository:
    backend = os.getenv("STORAGE_BACKEND", "file").lower()
    if backend == "memory":
        return MemoryRepository()
    if backend == "postgres":
        return PostgresRepository(os.getenv("DATABASE_URL", "postgresql://vertep@postgres/vertep"))
    return FileRepository(root)
