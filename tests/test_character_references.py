"""Reference images: storage, default use in image tasks, delivery to ComfyUI."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters import comfyui
from core import character_references as refs
from core.api import job_helpers
from core.app import app
from tests.test_gpu_media_fixtures import PNG_VALID


@pytest.fixture
def characters(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    (tmp_path / "hero").mkdir()
    return tmp_path


def test_rejects_non_images_and_too_many(characters):
    with pytest.raises(ValueError):
        refs.save_reference("hero", b"<html>not an image")
    for index in range(refs.MAX_REFERENCES):
        refs.save_reference("hero", PNG_VALID + bytes([index]))
    with pytest.raises(ValueError):
        refs.save_reference("hero", PNG_VALID + b"extra")
    assert refs.reference_path("hero", "../../secret.png") is None


def test_api_upload_list_preview_delete(characters):
    client = TestClient(app)
    created = client.post("/api/characters/hero/references", content=PNG_VALID)
    assert created.status_code == 200, created.text
    name = created.json()["filename"]
    assert client.get("/api/characters/hero/references").json()[0]["filename"] == name
    assert client.get(f"/api/characters/hero/references/{name}").content == PNG_VALID
    assert client.post("/api/characters/hero/references", content=b"bad").status_code == 422
    assert client.delete(f"/api/characters/hero/references/{name}").status_code == 200
    assert client.get("/api/characters/hero/references").json() == []


def test_image_task_uses_references_by_default(characters, monkeypatch):
    saved = refs.save_reference("hero", PNG_VALID)
    fields = job_helpers.reference_task_fields("hero", "workflows/image/demo.json", "image")
    assert fields["workflow"] == "workflows/image/ip-adapter.json"
    assert fields["character_references"] == [{"filename": saved["filename"], "sha256": saved["sha256"]}]
    assert job_helpers.reference_task_fields("hero", "workflows/image/my.json", "image")["workflow"] == "workflows/image/my.json"
    assert job_helpers.reference_task_fields("hero", "workflows/image/demo.json", "video") == {}
    assert job_helpers.reference_task_fields("other", "workflows/image/demo.json", "image") == {}


def _workflow():
    return json.loads(Path("workflows/image/ip-adapter.json").read_text(encoding="utf-8"))


def test_several_references_are_batched_into_one_input():
    workflow = comfyui.apply_references(_workflow(), ["a.png", "b.png", "c.png"])
    loads = [node for node in workflow.values() if node["class_type"] == "LoadImage"]
    assert sorted(node["inputs"]["image"] for node in loads) == ["a.png", "b.png", "c.png"]
    source = workflow["13"]["inputs"]["image"][0]
    assert workflow[source]["class_type"] == "ImageBatch"


def test_workflow_without_a_reference_input_fails_instead_of_ignoring_them():
    with pytest.raises(RuntimeError, match="REFERENCE_IMAGE"):
        comfyui.apply_references({"1": {"class_type": "SaveImage", "inputs": {}}}, ["a.png"])


def test_worker_fetches_verified_bytes_and_refuses_a_changed_file(monkeypatch):
    from worker import role_executor

    class Response:
        status_code = 200
        content = PNG_VALID
        text = ""
    monkeypatch.setenv("CORE_ADDRESS", "http://core")
    monkeypatch.setenv("NODE_NAME", "gpu")
    monkeypatch.setattr(role_executor.httpx, "get", lambda *a, **k: Response())
    import hashlib
    good = {"filename": "a.png", "sha256": hashlib.sha256(PNG_VALID).hexdigest()}
    task = {"task_id": "t", "character_references": [good]}
    assert role_executor._fetch_character_references(task) == [("a.png", PNG_VALID)]
    with pytest.raises(RuntimeError, match="digest"):
        role_executor._fetch_character_references({"task_id": "t", "character_references": [{**good, "sha256": "0"}]})
