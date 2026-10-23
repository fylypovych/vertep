"""Integration tests for Architecture 2.0 (Stage 9, Issue #300 i.0.0.3.0).

Tests the full cycle: Workflow Engine + Module System + Dispatcher +
backup/restore with module state and workflow checkpoints.
"""

import json
import tempfile
from pathlib import Path

from core.backup.resume import ResumeManager
from core.backup.snapshot import Snapshot
from core.backup.storage import SnapshotStorage
from core.dispatcher import Dispatcher
from core.dispatcher import TaskRequest
from core.module.discovery import ModuleDiscovery
from core.module.manifest import ModuleManifest
from core.module.registry import ModuleRegistry
from core.workflow.engine import WorkflowEngine
from core.workflow.state import WorkflowState


def test_integration_workflow_engine_module_system_dispatcher(tmp_path):
    """Full integration: create workflow, register modules, dispatch tasks."""
    # Workflow Engine
    state = WorkflowState(storage_backend="file", state_root=str(tmp_path / "wf-state"))
    engine = WorkflowEngine(state)
    definition = {
        "name": "integration-workflow",
        "tasks": [
            {"id": "text-task", "dependencies": [], "type": "text"},
            {"id": "image-task", "dependencies": ["text-task"], "type": "image"},
        ],
    }
    job_id = engine.create_job(definition)
    assert job_id is not None

    # Module System
    registry = ModuleRegistry()
    text_manifest = ModuleManifest(
        name="text", version="0.0.3.0",
        capabilities=["text_generation"],
        dependencies=["core_runtime"],
        resource_requirements={"min_vram_mb": 0},
    )
    image_manifest = ModuleManifest(
        name="image", version="0.0.3.0",
        capabilities=["image_generation"],
        dependencies=["core_runtime", "gpu_runtime"],
        resource_requirements={"min_vram_mb": 6144},
    )
    registry.register_module(text_manifest)
    registry.register_module(image_manifest)

    # Discovery
    discovery = ModuleDiscovery(registry=registry)
    discovery.report_worker_capabilities(
        "worker-text", ["text_generation"], {"vram_mb": 0, "cpu_cores": 1},
    )
    discovery.report_worker_capabilities(
        "worker-image", ["image_generation"], {"vram_mb": 8192, "cpu_cores": 2},
    )

    # Dispatcher
    dispatcher = Dispatcher()
    dispatcher.register_node("worker-text", {"text_generation"}, {"vram_mb": 0}, "text")
    dispatcher.register_node("worker-image", {"image_generation"}, {"vram_mb": 8192}, "gpu")

    # Dispatch text task
    text_task = TaskRequest(
        task_id="dispatch-text-1", task_type="text",
        capabilities_required={"text_generation"},
        shadow_mode=True,
    )
    text_response = dispatcher.assign_task(text_task)
    assert text_response.accepted is True
    assert text_response.worker_id == "worker-text"

    # Dispatch image task
    image_task = TaskRequest(
        task_id="dispatch-image-1", task_type="image",
        capabilities_required={"image_generation"},
        shadow_mode=True,
    )
    image_response = dispatcher.assign_task(image_task)
    assert image_response.accepted is True
    assert image_response.worker_id == "worker-image"

    # Verify module capabilities
    assert registry.has_capability("text_generation") is True
    assert registry.has_capability("image_generation") is True

    # Verify workflow state
    wf_status = engine.get_job_status(job_id)
    assert wf_status["status"] == "NEW"
    assert "text-task" in wf_status["tasks"]
    assert "image-task" in wf_status["tasks"]


def test_integration_backup_with_workflow_and_module_state(tmp_path):
    """Backup snapshot stores workflow checkpoints and module state."""
    storage = SnapshotStorage(root=str(tmp_path / "backups"))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[{"job_id": "wf-job-1", "status": "COMPLETED"}],
        tasks=[], artifacts=[], settings={}, version="0.0.3.0",
    )
    sid = storage.save_snapshot(snapshot)

    # Save module state
    manager.save_module_state(sid, "text", {"prompts_cached": 100})
    manager.save_module_state(sid, "image", {"models_cached": ["sd-v1"]})

    # Save workflow checkpoint
    checkpoint = {
        "job_id": "wf-job-1",
        "status": "COMPLETED",
        "tasks": {"t1": {"status": "COMPLETED"}, "t2": {"status": "COMPLETED"}},
        "progress": 100,
    }
    manager.save_workflow_checkpoint(sid, "wf-job-1", checkpoint)

    # Verify everything is in the snapshot
    loaded = storage.load_snapshot(sid)
    assert loaded.verify() is True
    assert loaded.module_state["text"] == {"prompts_cached": 100}
    assert loaded.module_state["image"] == {"models_cached": ["sd-v1"]}
    assert loaded.workflow_checkpoints["wf-job-1"] == checkpoint

    # Restore
    restored_module = manager.restore_module_state(sid, "text")
    assert restored_module == {"prompts_cached": 100}
    restored_wf = manager.restore_workflow_checkpoint(sid, "wf-job-1")
    assert restored_wf["progress"] == 100


def test_integration_selective_restore_with_modules(tmp_path):
    """Selective restore returns jobs and tasks filtered by ID."""
    storage = SnapshotStorage(root=str(tmp_path / "backups"))
    manager = ResumeManager(storage)
    snapshot = Snapshot.create(
        jobs=[
            {"job_id": "j1", "status": "READY"},
            {"job_id": "j2", "status": "FAILED"},
        ],
        tasks=[
            {"task_id": "t1", "job_id": "j1", "status": "COMPLETED"},
            {"task_id": "t2", "job_id": "j2", "status": "FAILED"},
        ],
        artifacts=[], settings={}, version="0.0.3.0",
    )
    sid = storage.save_snapshot(snapshot)

    result = manager.selective_restore(sid, ["j1"])
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["job_id"] == "j1"
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["job_id"] == "j1"
