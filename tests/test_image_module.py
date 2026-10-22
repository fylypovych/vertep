"""Тести для modules/image/ — Image Module логіка та модуль."""

from modules.image.image_logic import (
    default_image_params,
    normalize_media_artifact,
    validate_image_task,
    validate_video_task,
)


def test_validate_image_task_valid():
    task = {"topic": "кіт", "workflow": "workflows/image/demo.json"}
    assert validate_image_task(task) == []


def test_validate_image_task_missing_topic():
    task = {"workflow": "wf.json"}
    errors = validate_image_task(task)
    assert any("topic/prompt" in e for e in errors)


def test_validate_image_task_missing_workflow():
    task = {"topic": "кіт"}
    errors = validate_image_task(task)
    assert any("workflow" in e for e in errors)


def test_validate_video_task_valid():
    task = {"topic": "відео про кота"}
    assert validate_video_task(task) == []


def test_validate_video_task_missing_topic():
    task = {}
    errors = validate_video_task(task)
    assert any("topic/prompt" in e for e in errors)


def test_normalize_media_artifact():
    data = b"fake image bytes"
    artifact = normalize_media_artifact("scene-001.png", "image", data, {"workflow": "wf"})
    assert artifact["filename"] == "scene-001.png"
    assert artifact["kind"] == "image"
    assert artifact["size"] == len(data)
    assert artifact["metadata"]["workflow"] == "wf"


def test_default_image_params():
    task = {"topic": "кіт", "workflow": "custom.json"}
    params = default_image_params(task)
    assert params["workflow"] == "custom.json"
    assert params["topic"] == "кіт"
    assert params["task_type"] == "image"


class FakeProvider:
    def __init__(self, data=b"img", filename="out.png", kind="image"):
        self._data = data
        self._filename = filename
        self._kind = kind

    def generate_output(self, workflow, topic, task_type="image"):
        return self._data, self._filename, self._kind


def test_image_module_execute_image(monkeypatch):
    from modules.image.image_module import ImageModule
    module = ImageModule(provider=FakeProvider())
    artifacts = module.execute({"task": "image", "topic": "кіт", "workflow": "wf.json"})
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "image"
    assert artifacts[0]["filename"] == "out.png"


def test_image_module_health_check():
    from modules.image.image_module import ImageModule
    module = ImageModule()
    health = module.health_check()
    assert health["module"] == "image"
    assert health["status"] == "READY"


def test_image_module_resource_query():
    from modules.image.image_module import ImageModule
    module = ImageModule()
    res = module.resource_query()
    assert res["gpu_required"] is True
    assert res["min_vram_mb"] == 6144


def test_image_module_cancel_unknown_task():
    from modules.image.image_module import ImageModule
    module = ImageModule()
    assert module.cancel("nonexistent") is False


def test_image_module_execute_unknown_type_raises():
    from modules.image.image_module import ImageModule
    module = ImageModule()
    try:
        module.execute({"task": "unknown", "topic": "x"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown task type should raise ValueError")
