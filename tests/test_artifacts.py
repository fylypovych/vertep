from core.artifacts import register_artifact, verify_artifacts
from core.models import Job, JobStatus


def test_artifact_manifest_and_integrity(tmp_path):
    job = Job(job_id="2026-000001", topic="test", character_id="hero", priority=5,
              status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00")
    directory = tmp_path / job.job_id / "images"
    directory.mkdir(parents=True)
    image = directory / "scene-001.png"
    image.write_bytes(b"valid image placeholder")

    artifact = register_artifact(job, tmp_path, image, "image", scene_id="scene-001")

    assert artifact.sha256
    assert artifact.mime_type == "image/png"
    assert (tmp_path / job.job_id / "manifest.json").exists()
    assert verify_artifacts(job, tmp_path)[0]["valid"] is True

    image.write_bytes(b"tampered")
    assert verify_artifacts(job, tmp_path)[0]["valid"] is False
