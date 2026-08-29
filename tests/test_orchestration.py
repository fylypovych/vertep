import pytest

from core.models import Job, JobStatus, StageName, StageStatus
from core.orchestration import (all_scenes_ready, fail_scene, finish_scene,
                                initialize_plan, pending_scenes, recover_after_restart, start_scene,
                                transition_stage)
from core.script_schema import normalize_script


def planned_job():
    return Job(job_id="2026-777777", topic="DAG", character_id="did_samogon", priority=5,
               status=JobStatus.SCRIPT_READY, created_at="2026-01-01T00:00:00+00:00",
               script={"scenes": [{"prompt": "one", "duration": 1},
                                    {"prompt": "two", "voiceover": "voice", "duration": 2}]})


def test_plan_initializes_stages_and_scenes():
    job = initialize_plan(planned_job())
    assert list(job.stages) == ["SCRIPT", "ASSETS", "TTS", "ASSEMBLY", "PUBLISH"]
    assert job.stages["SCRIPT"].status == StageStatus.READY
    assert [scene.scene_id for scene in job.scenes] == ["scene-001", "scene-002"]
    assert job.scenes[1].voiceover == "voice"


def test_stage_transition_history_and_validation():
    job = Job(job_id="2026-777778", topic="DAG", character_id="did_samogon", priority=5,
              status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00")
    initialize_plan(job)
    transition_stage(job, StageName.SCRIPT, StageStatus.RUNNING)
    transition_stage(job, StageName.SCRIPT, StageStatus.FAILED, "failure")
    assert job.stages["SCRIPT"].attempts[0].error == "failure"
    with pytest.raises(ValueError):
        transition_stage(job, StageName.SCRIPT, StageStatus.READY)


def test_scene_lifecycle_and_fan_in():
    job = initialize_plan(planned_job())
    first, second = pending_scenes(job)
    start_scene(first, "task-1", "gpu-01")
    finish_scene(first, ["artifact-1"])
    start_scene(second, "task-2", "gpu-02")
    fail_scene(second, "out of memory")
    assert all_scenes_ready(job) is False
    assert pending_scenes(job) == [second]
    start_scene(second, "task-3", "gpu-03")
    finish_scene(second, ["artifact-2"])
    assert all_scenes_ready(job) is True


def test_restart_requeues_running_scene_and_closes_attempt():
    job = initialize_plan(planned_job())
    transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    scene = job.scenes[0]
    start_scene(scene, "task-1", "gpu-01")
    job.active_task_id = "task-1"
    job.active_task_ids["task-1"] = scene.scene_id
    job.assigned_worker = "gpu-01"

    recover_after_restart(job)

    assert scene.status == StageStatus.PENDING
    assert scene.task_id is None
    assert scene.attempts[-1].status == "FAILED"
    assert "restarted" in scene.attempts[-1].error
    assert job.active_task_ids == {}
    assert job.stages[StageName.ASSETS.value].status == StageStatus.FAILED


def test_script_normalization_adds_prompts_and_platform_metadata():
    script = normalize_script({"title": "Title", "hashtags": "#one #two", "voiceover": "Narration",
                               "scenes": [{"image_prompt": "still image", "duration": 2}]}, "Topic")
    assert script["scenes"][0]["prompt"] == "still image"
    assert script["scenes"][0]["video_prompt"] == "still image"
    assert script["scenes"][0]["voiceover"] == "Narration"
    assert script["platforms"]["youtube"]["hashtags"] == ["#one", "#two"]
    with pytest.raises(ValueError):
        normalize_script({"title": "Invalid", "scenes": []}, "Topic")
