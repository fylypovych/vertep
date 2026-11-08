"""#432 AC3/AC5: simultaneous claims on the real Redis queue and the order after a real CORE restart."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from tests.test_text_redis_process import contour, create, get_job, heartbeat, wait  # noqa: F401  (fixture)

NODES = ("text-a", "text-b", "text-c", "text-d")


def _claim(api, node):
    response = api.post("/api/tasks/claim", json={"node_name": node, "vram_mb": 0,
                                               "capabilities": ["text_generation"], "supported_tasks": ["text"]})
    assert response.status_code == 200, response.text
    return response.json()


def _finish(api, node, task):
    from worker.role_executor import execute_role_task
    response = api.post("/api/tasks/result", json={
        "node_name": node, "task_id": task["task_id"], "job_id": task["job_id"], "success": True,
        "artifacts": execute_role_task("text", task), "error": None})
    assert response.status_code == 200, response.text


def test_simultaneous_claims_on_redis_hand_each_task_to_one_worker(contour):  # noqa: F811
    api = contour["api"]
    for node in NODES:
        heartbeat(api, node=node)
    jobs = {create(api), create(api)}
    barrier = Barrier(len(NODES))

    def post(node):
        barrier.wait()
        return node, _claim(api, node)

    with ThreadPoolExecutor(len(NODES)) as pool:
        answers = list(pool.map(post, NODES))
    claimed = [(node, answer["task"]) for node, answer in answers if answer.get("task")]
    task_ids = [task["task_id"] for _, task in claimed]
    assert task_ids and len(task_ids) == len(set(task_ids)), answers
    assert {task["job_id"] for _, task in claimed} <= jobs
    assert len({node for node, _ in claimed}) == len(claimed), "a worker took two tasks"
    redis = contour["redis"]
    assert len(task_ids) + redis.zcard("vertep:tasks:ready") == len(jobs)


def test_the_turn_order_survives_a_real_core_restart(contour):  # noqa: F811
    api = contour["api"]
    for node in NODES[:2]:
        heartbeat(api, node=node)
    create(api)
    first = _claim(api, NODES[0])
    assert first.get("task"), first
    _finish(api, NODES[0], first["task"])
    job2 = create(api)
    # Restart only after CORE persisted the queued task on the Job: a restart in
    # between is a separate crash window, not the turn order this test proves.
    wait(lambda: any("SCRIPT TASK QUEUED" in event for event in get_job(api, job2).get("events", [])))

    contour["restart"]("core")
    for node in NODES[:2]:
        heartbeat(api, node=node)

    # The worker that took the last task yields its turn to the identical idle one,
    # also after the CORE process was restarted.
    assert _claim(api, NODES[0]) == {"task": None, "worker_state": "WAITING_FOR_TURN"}
    second = _claim(api, NODES[1])
    assert second.get("task", {}).get("job_id") == job2, second
