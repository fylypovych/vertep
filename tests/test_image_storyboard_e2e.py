import base64, time
from fastapi.testclient import TestClient
from core.app import app, store
from core.models import JobStatus, StoryboardScene, StoryboardVersion
from worker import role_executor
from core.script_agent import ScriptAgent

def _mock_queue(self, job, revision=None):
    tgt = getattr(self, "store", store)
    script = job.script or {"title": job.topic, "scenes": [{"prompt": job.topic, "voiceover": "", "duration": 1}]}
    scenes = [StoryboardScene(index=i, prompt=s.get("prompt",""), video_prompt=s.get("prompt",""), voiceover=s.get("voiceover",""), duration=float(s.get("duration",1))) for i,s in enumerate(script.get("scenes",[]),1)]
    sb = StoryboardVersion(version=(job.storyboards[-1].version+1 if job.storyboards else 1), title=script.get("title",job.topic), description=script.get("description",""), hashtags=script.get("hashtags",[]), scenes=scenes, status="pending_approval", image_status="pending", image_version=1)
    for sc in sb.scenes:
        sc.scene_id=f"sb-{sb.version}-{sc.index}"; sc.image_prompt=sc.prompt; sc.image_version=1
    job.storyboards.append(sb); job.active_storyboard_version=sb.version; job.active_image_version=sb.image_version
    job.storyboard_task_id = "mock-task-id"
    tgt.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL, f"STORYBOARD {sb.version} PENDING APPROVAL")
    from core.image_storyboard import queue_image_storyboard
    queue_image_storyboard(tgt, job, sb.version)
    return sb

def test_image_storyboard_e2e(monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK","false")
    from core.storyboard import StoryboardService as Svc
    monkeypatch.setattr(Svc, "queue", _mock_queue)
    client=TestClient(app)
    client.post("/api/workers/heartbeat", json={"node_name":"gpu-worker","vram_mb":8192,"capabilities":["image_generation"],"supported_tasks":["image"]})
    client.post("/api/workers/heartbeat", json={"node_name":"text-worker","vram_mb":0,"role":"text","capabilities":["text_generation"],"supported_tasks":["text"]})
    monkeypatch.setattr(ScriptAgent, "generate_script", lambda self, topic, system_prompt="", character=None: {
        "title": topic, "scenes": [{"prompt": topic, "voiceover": "", "duration": 1}]})
    job_id=client.post("/api/jobs", json={"topic":"E2E storyboard","character_id":"did_samogon"}).json()["job_id"]
    for _ in range(100):
        j=client.get(f"/api/jobs/{job_id}").json()
        if j["status"]=="SCRIPT_QUEUED":
            resp=client.post("/api/tasks/claim", json={"node_name":"text-worker","vram_mb":0})
            task=resp.json().get("task")
            if task and task.get("task")=="script":
                [artifact]=role_executor.execute_role_task("text", task)
                client.post("/api/tasks/result", json={"job_id":job_id,"task_id":task["task_id"],"node_name":"text-worker","success":True,"artifacts":[artifact]})
        if j["status"]=="SCRIPT_PENDING_APPROVAL": break
        time.sleep(0.02)
    assert j["status"]=="SCRIPT_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/script/approve", json={"actor":"test"})
    for _ in range(100):
        j=client.get(f"/api/jobs/{job_id}").json()
        if j["status"]=="STORYBOARD_PENDING_APPROVAL": break
        time.sleep(0.02)
    assert j["status"]=="STORYBOARD_PENDING_APPROVAL"
    sb=client.get(f"/api/jobs/{job_id}/storyboards").json()[0]
    assert sb["image_status"] in ("pending","generating")
    tasks=[]
    for _ in range(50):
        t=client.post("/api/tasks/claim", json={"node_name":"gpu-worker","vram_mb":8192}).json().get("task")
        if t and t.get("kind")=="image_storyboard": tasks.append(t)
        if len(tasks)>=len(sb["scenes"]): break
        time.sleep(0.01)
    assert tasks
    ppm=b"P6\n2 2\n255\n"+bytes((10,20,30))*4
    for t in tasks:
        r=client.post("/api/tasks/result", json={"job_id":job_id,"task_id":t["task_id"],"node_name":"gpu-worker","success":True,"artifacts":[{"filename":"preview.ppm","data_base64":base64.b64encode(ppm).decode()}]})
        print("RESULT", r.status_code, r.text)
        assert r.status_code==200
    for _ in range(50):
        sb=client.get(f"/api/jobs/{job_id}/storyboards").json()[0]
        if sb["image_status"]=="ready": break
        time.sleep(0.02)
    assert sb["image_status"]=="ready"
    assert all(s.get("image_artifact_id") for s in sb["scenes"])
    rev_sb=client.post(f"/api/jobs/{job_id}/storyboards/images/revision", json={"version":sb["version"],"actor":"test","scene_indexes":[1],"revision":"new prompt"}).json()
    sb2=client.get(f"/api/jobs/{job_id}/storyboards").json()[0]
    assert sb2["image_version"]==sb["image_version"]+1
    t2=None
    for _ in range(50):
        t=client.post("/api/tasks/claim", json={"node_name":"gpu-worker","vram_mb":8192}).json().get("task")
        if t and t.get("kind")=="image_storyboard": t2=t; break
        time.sleep(0.01)
    assert t2
    client.post("/api/tasks/result", json={"job_id":job_id,"task_id":t2["task_id"],"node_name":"gpu-worker","success":True,"artifacts":[{"filename":"preview2.ppm","data_base64":base64.b64encode(ppm).decode()}]})
    for _ in range(50):
        sb2=client.get(f"/api/jobs/{job_id}/storyboards").json()[0]
        if sb2["image_status"]=="ready": break
        time.sleep(0.02)
    assert sb2["image_status"]=="ready"
    client.post(f"/api/jobs/{job_id}/storyboards/images/approve", json={"version":sb2["version"],"actor":"test"})
    sb3=client.get(f"/api/jobs/{job_id}/storyboards").json()[0]
    assert sb3["image_status"]=="approved"
    r=client.post(f"/api/jobs/{job_id}/storyboards/approve", json={"version":sb3["version"],"actor":"test"})
    assert r.status_code==200
    # Need image worker to complete asset generation
    for _ in range(80):
        j=client.get(f"/api/jobs/{job_id}").json()
        if j["status"]=="READY": break
        # dispatch may need worker claim
        t=client.post("/api/tasks/claim", json={"node_name":"gpu-worker","vram_mb":8192}).json().get("task")
        if t and t.get("task_id"):
            ppm2=b"P6\n2 2\n255\n"+bytes((30,40,50))*4
            client.post("/api/tasks/result", json={"job_id":job_id,"task_id":t["task_id"],"node_name":"gpu-worker","success":True,"artifacts":[{"filename":"scene.ppm","data_base64":base64.b64encode(ppm2).decode()}]})
        time.sleep(0.05)
    assert j["status"]=="READY"
