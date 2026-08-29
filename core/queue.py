import heapq
import json
import os
import threading
import time
import uuid


class TaskQueue:
    """Priority queue with explicit leases and Redis/local implementations."""

    CLAIM_LUA = """
local deferred = {}
for i=1,100 do
  local row = redis.call('ZPOPMIN', KEYS[1], 1)
  if #row == 0 then break end
  local item = cjson.decode(row[1])
  if item['not_before'] and tonumber(item['not_before']) > tonumber(ARGV[1]) then
    table.insert(deferred, {raw=row[1], score=row[2]})
  else
    for _,held in ipairs(deferred) do redis.call('ZADD', KEYS[1], held['score'], held['raw']) end
    redis.call('HSET', KEYS[2], item['task_id'], cjson.encode({lease_until=tonumber(ARGV[2]), task=item}))
    return row[1]
  end
end
for _,held in ipairs(deferred) do redis.call('ZADD', KEYS[1], held['score'], held['raw']) end
return nil
"""
    RENEW_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local record = cjson.decode(raw)
record['lease_until'] = tonumber(ARGV[2])
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(record))
return 1
"""
    REQUEUE_LUA = """
local rows = redis.call('HGETALL', KEYS[2])
local expired = {}
for index=1,#rows,2 do
  local record = cjson.decode(rows[index+1])
  if tonumber(record['lease_until']) <= tonumber(ARGV[1]) then
    local task = record['task']
    local score = -tonumber(task['priority'] or 5) * 10000000000 + tonumber(task['enqueued_at'] or ARGV[1])
    redis.call('HDEL', KEYS[2], rows[index])
    redis.call('ZADD', KEYS[1], score, cjson.encode(task))
    table.insert(expired, cjson.encode(task))
  end
end
return expired
"""
    RELEASE_LUA = """
local raw = redis.call('HGET', KEYS[2], ARGV[1])
if not raw then return nil end
local record = cjson.decode(raw)
local task = record['task']
local score = -tonumber(task['priority'] or 5) * 10000000000 + tonumber(task['enqueued_at'] or ARGV[2])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('ZADD', KEYS[1], score, cjson.encode(task))
return cjson.encode(task)
"""

    def __init__(self) -> None:
        self._local: list[tuple[int, int, dict]] = []
        self._inflight: dict[str, tuple[float, dict]] = {}
        self._sequence = 0
        self._lock = threading.Lock()
        self._cancellations: dict[str, list[dict]] = {}
        self._dead_letters: list[dict] = []
        self._redis = None
        try:
            import redis
            candidate = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
            candidate.ping()
            self._redis = candidate
        except Exception:
            self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis else "local"

    def enqueue(self, task: dict, *, new_attempt: bool = False) -> dict:
        item = dict(task)
        if new_attempt or not item.get("task_id"):
            item["task_id"] = uuid.uuid4().hex
        if new_attempt:
            item["enqueued_at"] = time.time()
        item.setdefault("priority", 5)
        item.setdefault("enqueued_at", time.time())
        if self._redis:
            score = -int(item["priority"]) * 10_000_000_000 + float(item["enqueued_at"])
            self._redis.zadd("vertep:tasks:ready", {json.dumps(item, sort_keys=True): score})
        else:
            with self._lock:
                self._sequence += 1
                heapq.heappush(self._local, (-int(item["priority"]), self._sequence, item))
        return item

    def depth(self) -> int:
        return int(self._redis.zcard("vertep:tasks:ready")) if self._redis else len(self._local)

    def inflight_depth(self) -> int:
        return int(self._redis.hlen("vertep:tasks:inflight")) if self._redis else len(self._inflight)

    def claim(self, lease_seconds: int | None = None) -> dict | None:
        lease = lease_seconds or int(os.getenv("TASK_LEASE_SECONDS", "90"))
        if self._redis:
            now = time.time()
            raw = self._redis.eval(self.CLAIM_LUA, 2, "vertep:tasks:ready", "vertep:tasks:inflight",
                                   now, now + lease)
            if not raw:
                return None
            return json.loads(raw)
        with self._lock:
            if not self._local:
                return None
            deferred = []
            item = None
            while self._local:
                row = heapq.heappop(self._local)
                if float(row[2].get("not_before", 0)) <= time.time():
                    item = row[2]
                    break
                deferred.append(row)
            for row in deferred:
                heapq.heappush(self._local, row)
            if item is None:
                return None
            self._inflight[item["task_id"]] = (time.time() + lease, item)
            return item

    def ack(self, task_id: str) -> None:
        if self._redis:
            self._redis.hdel("vertep:tasks:inflight", task_id)
        else:
            with self._lock:
                self._inflight.pop(task_id, None)

    def renew(self, task_id: str, lease_seconds: int | None = None) -> bool:
        deadline = time.time() + (lease_seconds or int(os.getenv("TASK_LEASE_SECONDS", "90")))
        if self._redis:
            return bool(self._redis.eval(self.RENEW_LUA, 1, "vertep:tasks:inflight", task_id, deadline))
        with self._lock:
            record = self._inflight.get(task_id)
            if not record:
                return False
            self._inflight[task_id] = (deadline, record[1])
            return True

    def acquire_watchdog_lock(self, ttl_seconds: int = 10) -> bool:
        if not self._redis:
            return True
        return bool(self._redis.set("vertep:watchdog:lock", uuid.uuid4().hex, nx=True, ex=ttl_seconds))

    def request_cancel(self, node_name: str, task_id: str) -> None:
        item = {"task_id": task_id, "requested_at": time.time()}
        if self._redis:
            self._redis.rpush(f"vertep:cancellations:{node_name}", json.dumps(item))
            self._redis.expire(f"vertep:cancellations:{node_name}", 3600)
        else:
            self._cancellations.setdefault(node_name, []).append(item)

    def pop_cancellations(self, node_name: str) -> list[dict]:
        if self._redis:
            key = f"vertep:cancellations:{node_name}"
            pipe = self._redis.pipeline()
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            rows, _ = pipe.execute()
            return [json.loads(row) for row in rows]
        return self._cancellations.pop(node_name, [])

    def discard(self, task_id: str) -> None:
        self.ack(task_id)
        if self._redis:
            for raw in self._redis.zrange("vertep:tasks:ready", 0, -1):
                if json.loads(raw).get("task_id") == task_id:
                    self._redis.zrem("vertep:tasks:ready", raw)
        else:
            with self._lock:
                self._local = [row for row in self._local if row[2].get("task_id") != task_id]
                heapq.heapify(self._local)

    def release(self, task_id: str) -> dict | None:
        if self._redis:
            raw = self._redis.eval(self.RELEASE_LUA, 2, "vertep:tasks:ready", "vertep:tasks:inflight",
                                   task_id, time.time())
            return json.loads(raw) if raw else None
        item = self._take_inflight(task_id)
        return self.enqueue(item) if item else None

    def requeue_expired(self, now: float | None = None) -> list[dict]:
        current = now or time.time()
        expired: list[dict] = []
        if self._redis:
            rows = self._redis.eval(self.REQUEUE_LUA, 2, "vertep:tasks:ready", "vertep:tasks:inflight", current)
            return [json.loads(row) for row in rows]
        with self._lock:
            task_ids = [task_id for task_id, (deadline, _) in self._inflight.items() if deadline <= current]
            items = [self._inflight.pop(task_id)[1] for task_id in task_ids]
        for item in items:
            expired.append(self.enqueue(item))
        return expired

    def _take_inflight(self, task_id: str) -> dict | None:
        if self._redis:
            raw = self._redis.hget("vertep:tasks:inflight", task_id)
            if not raw:
                return None
            self._redis.hdel("vertep:tasks:inflight", task_id)
            return json.loads(raw)["task"]
        with self._lock:
            record = self._inflight.pop(task_id, None)
            return record[1] if record else None

    def dead_letter(self, task: dict, error: str | None = None) -> dict:
        item = dict(task)
        item.update({"error": error, "failed_at": time.time()})
        if self._redis:
            self._redis.rpush("vertep:tasks:dead", json.dumps(item, sort_keys=True))
        else:
            with self._lock:
                self._dead_letters.append(item)
        return item

    def dead_letters(self) -> list[dict]:
        if self._redis:
            return [json.loads(row) for row in self._redis.lrange("vertep:tasks:dead", 0, -1)]
        with self._lock:
            return [dict(item) for item in self._dead_letters]

    def requeue_dead_letter(self, task_id: str) -> dict | None:
        if self._redis:
            rows = self._redis.lrange("vertep:tasks:dead", 0, -1)
            for index, raw in enumerate(rows):
                item = json.loads(raw)
                if item.get("task_id") == task_id:
                    self._redis.lset("vertep:tasks:dead", index, "__removed__")
                    self._redis.lrem("vertep:tasks:dead", 1, "__removed__")
                    item.pop("error", None)
                    item.pop("failed_at", None)
                    return self.enqueue(item, new_attempt=True)
            return None
        with self._lock:
            index = next((i for i, item in enumerate(self._dead_letters) if item.get("task_id") == task_id), None)
            item = self._dead_letters.pop(index) if index is not None else None
        if item:
            item.pop("error", None)
            item.pop("failed_at", None)
            return self.enqueue(item, new_attempt=True)
        return None
