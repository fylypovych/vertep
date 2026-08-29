from .models import (AttemptRecord, Job, SceneRecord, StageName, StageRecord,
                     StageStatus, utc_now)

STAGE_ORDER = [StageName.SCRIPT, StageName.ASSETS, StageName.TTS, StageName.ASSEMBLY, StageName.PUBLISH]
ALLOWED_TRANSITIONS = {
    StageStatus.PENDING: {StageStatus.RUNNING, StageStatus.CANCELLED},
    StageStatus.RUNNING: {StageStatus.READY, StageStatus.FAILED, StageStatus.PAUSED, StageStatus.CANCELLED},
    StageStatus.FAILED: {StageStatus.RUNNING, StageStatus.CANCELLED},
    StageStatus.PAUSED: {StageStatus.RUNNING, StageStatus.CANCELLED},
    StageStatus.READY: {StageStatus.RUNNING},
    StageStatus.CANCELLED: set(),
}


def initialize_plan(job: Job) -> Job:
    new_plan = not job.stages
    if new_plan:
        job.stages = {name.value: StageRecord(name=name) for name in STAGE_ORDER}
        if job.script:
            job.stages[StageName.SCRIPT.value].status = StageStatus.READY
    if job.script and not job.scenes:
        job.scenes = [SceneRecord(
            scene_id=f"scene-{index:03d}", index=index,
            prompt=str(scene.get("prompt") or job.topic),
            video_prompt=scene.get("video_prompt"),
            voiceover=str(scene.get("voiceover") or scene.get("text") or ""),
            duration=float(scene.get("duration", 5)),
        ) for index, scene in enumerate(job.script.get("scenes") or [{"prompt": job.topic}], 1)]
    return job


def transition_stage(job: Job, name: StageName, status: StageStatus, error: str | None = None) -> StageRecord:
    initialize_plan(job)
    stage = job.stages[name.value]
    if status == stage.status:
        return stage
    if status not in ALLOWED_TRANSITIONS[stage.status]:
        raise ValueError(f"Invalid {name.value} transition: {stage.status.value} -> {status.value}")
    now = utc_now()
    if status == StageStatus.RUNNING:
        stage.started_at = now
        stage.completed_at = None
        stage.attempts.append(AttemptRecord(attempt=len(stage.attempts) + 1, status="RUNNING", started_at=now))
    elif status in {StageStatus.READY, StageStatus.FAILED, StageStatus.CANCELLED}:
        stage.completed_at = now
        if stage.attempts:
            stage.attempts[-1].status = status.value
            stage.attempts[-1].completed_at = now
            stage.attempts[-1].error = error
    stage.status = status
    job.version += 1
    return stage


def pending_scenes(job: Job) -> list[SceneRecord]:
    initialize_plan(job)
    return [scene for scene in job.scenes if scene.status in {StageStatus.PENDING, StageStatus.FAILED}]


def start_scene(scene: SceneRecord, task_id: str, node_name: str | None = None) -> None:
    now = utc_now()
    scene.status = StageStatus.RUNNING
    scene.task_id = task_id
    scene.assigned_worker = node_name
    scene.attempts.append(AttemptRecord(attempt=len(scene.attempts) + 1, status="RUNNING",
                                        started_at=now, node_name=node_name))


def finish_scene(scene: SceneRecord, artifact_ids: list[str]) -> None:
    now = utc_now()
    scene.status = StageStatus.READY
    scene.artifact_ids.extend(item for item in artifact_ids if item not in scene.artifact_ids)
    if scene.attempts:
        scene.attempts[-1].status = "READY"
        scene.attempts[-1].completed_at = now


def fail_scene(scene: SceneRecord, error: str) -> None:
    now = utc_now()
    scene.status = StageStatus.FAILED
    if scene.attempts:
        scene.attempts[-1].status = "FAILED"
        scene.attempts[-1].completed_at = now
        scene.attempts[-1].error = error


def interrupt_scene(scene: SceneRecord, error: str) -> None:
    """Close a lost lease attempt and make the same scene dispatchable again."""
    fail_scene(scene, error)
    scene.status = StageStatus.PENDING
    scene.assigned_worker = None


def cancel_scene(scene: SceneRecord, reason: str) -> None:
    now = utc_now()
    scene.status = StageStatus.CANCELLED
    scene.assigned_worker = None
    if scene.attempts and scene.attempts[-1].status == "RUNNING":
        scene.attempts[-1].status = "CANCELLED"
        scene.attempts[-1].completed_at = now
        scene.attempts[-1].error = reason


def all_scenes_ready(job: Job) -> bool:
    return bool(job.scenes) and all(scene.status == StageStatus.READY for scene in job.scenes)


def recover_after_restart(job: Job) -> Job:
    """Make persisted in-progress state safely dispatchable after process restart."""
    now = utc_now()
    job.active_task_id = None
    job.active_task_ids.clear()
    job.assigned_worker = None
    for scene in job.scenes:
        scene.task_id = None
        scene.assigned_worker = None
        if scene.status == StageStatus.RUNNING:
            scene.status = StageStatus.PENDING
            if scene.attempts and scene.attempts[-1].status == "RUNNING":
                scene.attempts[-1].status = "FAILED"
                scene.attempts[-1].completed_at = now
                scene.attempts[-1].error = "CORE restarted while task was running"
    for stage in job.stages.values():
        if stage.status == StageStatus.RUNNING:
            stage.status = StageStatus.FAILED
            stage.completed_at = now
            if stage.attempts and stage.attempts[-1].status == "RUNNING":
                stage.attempts[-1].status = "FAILED"
                stage.attempts[-1].completed_at = now
                stage.attempts[-1].error = "CORE restarted while stage was running"
    job.version += 1
    return job
