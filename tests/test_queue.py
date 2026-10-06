import time

from core.queue import TaskQueue


def test_priority_and_lease_requeue():
    queue = TaskQueue()
    assert queue.backend == "local"
    low = queue.enqueue({"job_id": "low", "priority": 1})
    high = queue.enqueue({"job_id": "high", "priority": 10})
    assert queue.claim(lease_seconds=1)["task_id"] == high["task_id"]
    claimed = queue.claim(lease_seconds=1)
    assert claimed["task_id"] == low["task_id"]
    expired = queue.requeue_expired(now=time.time() + 2)
    assert {item["task_id"] for item in expired} == {high["task_id"], low["task_id"]}
    assert queue.inflight_depth() == 0


def test_discard_removes_ready_task():
    queue = TaskQueue()
    assert queue.backend == "local"
    task = queue.enqueue({"job_id": "cancelled", "priority": 5})
    queue.discard(task["task_id"])
    assert queue.depth() == 0


def test_lease_renewal_and_cancellation_channel():
    queue = TaskQueue()
    assert queue.backend == "local"
    task = queue.enqueue({"job_id": "leased", "priority": 5})
    claimed = queue.claim(lease_seconds=1)
    assert claimed["task_id"] == task["task_id"]
    assert queue.renew(task["task_id"], lease_seconds=30) is True
    assert queue.requeue_expired(now=time.time() + 2) == []
    queue.request_cancel("gpu-01", task["task_id"])
    assert queue.pop_cancellations("gpu-01")[0]["task_id"] == task["task_id"]
    assert queue.pop_cancellations("gpu-01") == []


def test_dead_letter_can_be_requeued():
    queue = TaskQueue()
    assert queue.backend == "local"
    task = queue.enqueue({"job_id": "failed", "scene_id": "scene-001", "priority": 5})
    queue.dead_letter(task, "GPU error")
    assert queue.dead_letters()[0]["error"] == "GPU error"
    retried = queue.requeue_dead_letter(task["task_id"])
    assert retried["task_id"] != task["task_id"]
    assert queue.dead_letters() == []


def test_future_high_priority_task_does_not_block_due_task():
    queue = TaskQueue()
    assert queue.backend == "local"
    future = queue.enqueue({"job_id": "future", "priority": 10, "not_before": time.time() + 3600})
    due = queue.enqueue({"job_id": "due", "priority": 1})

    assert queue.claim()["task_id"] == due["task_id"]
    assert queue.claim() is None
    assert queue.depth() == 1
    assert future["task_id"] != due["task_id"]
