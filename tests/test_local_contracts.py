import json
import subprocess
import sys
import os
import stat
import time
import httpx

from fastapi.testclient import TestClient

from adapters.tts import TTSAdapter
from adapters.ffmpeg import FFmpegAdapter
from adapters.llm import LLMAdapter
from core.app import _hash_secret, app
from core.models import Job, JobStatus, StageName, StageStatus
from core.pipeline import prepare_job
from core.repository import FileRepository, MemoryRepository
from core.maintenance import cleanup_jobs, cleanup_temporary_files
from core.workflows import WorkflowRegistry, validate_workflow
from worker.service import gpu_info, submit_result, worker_status


def sample_job():
    return Job(job_id="2026-123456", topic="Repository", character_id="did_samogon", priority=5,
               status=JobStatus.NEW, created_at="2026-01-01T00:00:00+00:00")


def test_memory_and_atomic_file_repositories(tmp_path):
    job = sample_job()
    memory = MemoryRepository()
    memory.save_job(job)
    job.status = JobStatus.FAILED
    assert list(memory.load_jobs())[0].status == JobStatus.NEW
    files = FileRepository(tmp_path)
    files.save_job(job)
    assert list(files.load_jobs())[0].status == JobStatus.FAILED
    assert not list(tmp_path.rglob("*.tmp"))
    files.append_event(job.job_id, job.created_at, "TEST EVENT")
    task = {"task_id": "task-1", "job_id": job.job_id, "task": "image"}
    files.record_task(task, "QUEUED")
    files.record_telegram_update("chat", "message", {"ok": True})
    assert files.has_telegram_update("chat", "message") is True
    assert (tmp_path / job.job_id / "events.jsonl").is_file()
    assert memory.next_job_sequence(2026, 41) == 42


def test_mock_tts_creates_wav_manifest(tmp_path):
    output = tmp_path / "voice.wav"
    result = TTSAdapter("mock").synthesize("Тест", output, 0.2, {"voice": "mock"})
    assert result["status"] == "READY"
    assert output.stat().st_size > 100
    assert output.with_suffix(".json").is_file()


def test_tts_stage_retries_until_success(monkeypatch, tmp_path):
    from core.pipeline import JobStore

    monkeypatch.setenv("TTS_PROVIDER", "test")
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path / "characters"))
    monkeypatch.setattr(LLMAdapter, "generate_script", lambda self, topic, system_prompt="": {
        "title": topic, "scenes": [{"prompt": topic, "voiceover": "hello", "duration": 1}]})
    calls = {"count": 0}

    def synthesize(self, text, output, duration=3, voice=None):
        calls["count"] += 1
        if calls["count"] < 3:
            return {"status": "FAILED", "error": "temporary TTS error"}
        output.write_bytes(b"RIFFtest-wave")
        return {"status": "READY"}

    monkeypatch.setattr(TTSAdapter, "synthesize", synthesize)
    monkeypatch.setattr(FFmpegAdapter, "concat_audio",
                        lambda self, sources, output: output.write_bytes(b"RIFFjoined") or output)
    store = JobStore(str(tmp_path), MemoryRepository())
    job = store.create("TTS retries", "did_samogon", 5)

    prepare_job(store, job)

    stage = job.stages[StageName.TTS.value]
    assert calls["count"] == 3
    assert [attempt.status for attempt in stage.attempts] == ["FAILED", "FAILED", "READY"]
    assert stage.status == StageStatus.READY


def test_workflow_registry_validation(tmp_path):
    registry = WorkflowRegistry(tmp_path)
    workflow = {"1": {"class_type": "SaveImage", "inputs": {"text": "{{TOPIC}}"}}}
    assert validate_workflow(workflow) == []
    registry.save("image", "test.json", workflow)
    assert registry.list()[0]["valid"] is True
    assert registry.load("image", "test.json") == workflow


def test_roles_metrics_and_worker_log_ingestion(monkeypatch):
    password = "viewer-password-long"
    monkeypatch.setenv("ADMIN_PASSWORD", "fallback-admin-password")
    monkeypatch.setenv("USERS_JSON", json.dumps({"reader": {"password_hash": _hash_secret(password), "role": "viewer"}}))
    monkeypatch.setenv("WORKER_TOKENS", "log-worker:log-secret")
    client = TestClient(app)
    metrics = client.get("/api/metrics", auth=("reader", password))
    assert metrics.status_code == 200
    assert {"queue_dead_letter", "jobs_scheduled", "scenes_by_status"} <= metrics.json().keys()
    assert client.post("/api/jobs", json={"topic": "forbidden"}, auth=("reader", password)).status_code == 403
    response = client.post("/api/logs/ingest", json={"node_name": "log-worker", "entries": [{"level": "INFO", "message": "hello"}]},
                           headers={"X-Vertep-Token": "log-secret"})
    assert response.status_code == 200


def test_installer_enables_services_and_installs_cli():
    installer = open("install.sh", encoding="utf-8").read()
    assert "systemctl enable --now vertep-core.service" in installer
    assert "systemctl enable --now vertep-worker.service" in installer
    assert 'ln -sfn "$ROOT_DIR/scripts/vertep" /usr/local/bin/vertep' in installer
    assert "VERTEP_ROLE" in installer
    assert "vertep-update.path" in installer
    assert "WEB_UPDATE_ENABLED true" in installer


def test_installer_waits_for_working_nvidia_driver_before_worker_start():
    installer = open("install.sh", encoding="utf-8").read()
    assert "if ! nvidia-smi >/dev/null 2>&1" in installer
    assert "worker_ready=false" in installer
    assert 'if [[ "$worker_ready" == true ]]' in installer
    assert "GPU is not ready; ComfyUI and vertep-worker" in installer


def test_worker_compose_is_standalone_and_role_plan_is_manifest_driven():
    worker_compose = open("docker-compose.worker.yml", encoding="utf-8").read()
    assert "postgres:" not in worker_compose
    assert "redis:" not in worker_compose
    assert "depends_on:" not in worker_compose
    assert "network_mode: host" in worker_compose
    assert "COMFYUI_URL: http://127.0.0.1:8188" in worker_compose
    result = subprocess.run([sys.executable, "installer/role-plan.py", "installer/manifest.json",
                             "core-worker", "services"], check=True, capture_output=True, text=True)
    assert {"vertep-core", "vertep-worker", "vertep-comfyui"} <= set(result.stdout.splitlines())
    manifest = json.load(open("installer/manifest.json", encoding="utf-8"))
    assert manifest["profiles"]["core-worker"]["includes"] == ["core", "gpu"]
    assert 'choices=["core", "worker", "core-worker"]' not in open("installer/role-plan.py", encoding="utf-8").read()


def test_status_renderer_survives_offline_core():
    result = subprocess.run([sys.executable, "scripts/status.py"], input="{}", check=True,
                            capture_output=True, text=True)
    assert "VERTEP" in result.stdout


def test_installer_firewall_and_ssh_hardening_are_lockout_safe():
    installer = open("install.sh", encoding="utf-8").read()
    assert "ufw default deny incoming" in installer
    assert '[[ -s "$authorized_keys" ]]' in installer
    assert "PasswordAuthentication no" in installer
    assert "sshd -t" in installer
    assert "password authentication was not disabled" in installer


def test_update_applies_migrations_before_core_rebuild():
    script = open("scripts/vertep", encoding="utf-8").read()
    migration = script.index('psql -v ON_ERROR_STOP=1 -U vertep -d vertep < "$migration"')
    core_rebuild = script.index('up -d core redis postgres')
    assert script.index("pg_isready") < migration < core_rebuild
    assert "systemctl enable --now vertep-update.path" in script


def test_web_ui_is_utf8_and_has_orchestration_sections():
    html = open("web/index.html", encoding="utf-8").read()
    assert "Панель керування" in html
    assert "Dead-letter queue" in html
    assert "Етапи та сцени" in html
    assert "РќР" not in html
    assert "вЂ" not in html


def test_web_ui_has_remaining_job_and_registry_controls():
    html = open("web/index.html", encoding="utf-8").read()
    assert "Retry publish" in html
    assert "removeJob" in html
    assert 'data-panel="brands"' in html
    assert 'data-panel="workflows"' in html
    assert "mime_type?.startsWith('video/')" in html
    assert "Безпечне оновлення Vertep" in html
    assert "requestSystemUpdate" in html


def test_worker_converts_rejected_artifact_to_failed_result():
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(400 if len(payloads) == 1 else 200, text="bad signature")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        submit_result(client, "http://core", {"job_id": "job", "task_id": "task", "node_name": "gpu",
                                              "success": True, "images": []})
    assert len(payloads) == 2
    assert payloads[1]["success"] is False
    assert "rejected" in payloads[1]["error"]


def test_worker_reports_error_when_required_gpu_is_unavailable():
    assert worker_status({"gpu_available": False}, require_gpu=True) == "ERROR"
    assert worker_status({"gpu_available": False}, require_gpu=False) == "READY"
    assert worker_status({"gpu_available": True}, require_gpu=True, busy=True) == "BUSY"


def test_gtx_1660_installer_profile_is_turing_low_vram():
    result = subprocess.run([sys.executable, "installer/gpu-profile.py", "--name",
                             "NVIDIA GeForce GTX 1660", "--vram-mb", "6144",
                             "--compute-capability", "7.5"], check=True, capture_output=True, text=True)
    profile = json.loads(result.stdout)
    assert profile["profile_id"] == "gtx1660-6gb"
    assert profile["architecture"] == "Turing"
    assert profile["compute_capability"] == "7.5"
    assert profile["vram_mb"] == 6144
    assert profile["supported"] is True
    assert profile["comfyui_args"] == "--lowvram"
    assert profile["torch_index_url"].endswith("/cu124")
    assert "torch==2.6.0" in profile["torch_packages"]
    assert profile["recommended_tasks"] == ["image"]


def test_worker_heartbeat_exposes_gtx_1660_profile(monkeypatch):
    class Result:
        stdout = "NVIDIA GeForce GTX 1660, 6144, 5980, 51, 12, 550.54.14, 7.5\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    metrics = gpu_info()
    assert metrics["gpu_available"] is True
    assert metrics["gpu_profile"] == "gtx1660-6gb"
    assert metrics["gpu_architecture"] == "Turing"
    assert metrics["compute_capability"] == "7.5"
    assert metrics["driver_version"] == "550.54.14"


def test_comfyui_worker_is_loopback_only_and_uses_gpu_profile():
    unit = open("installer/vertep-comfyui.service", encoding="utf-8").read()
    installer = open("installer/install-comfyui.sh", encoding="utf-8").read()
    assert "--listen 127.0.0.1" in unit
    assert "EnvironmentFile=-/etc/vertep/gpu.env" in unit
    assert "$COMFYUI_ARGS" in unit
    assert "torch.cuda.is_available()" in installer
    assert "gpu-profile.py" in installer


def test_generate_and_upgrade_env(tmp_path):
    template = tmp_path / ".env.example"
    output = tmp_path / ".env"
    template.write_text("ADMIN_PASSWORD=replace\nNODE_API_TOKEN=replace\nPOSTGRES_PASSWORD=replace\nTELEGRAM_WEBHOOK_SECRET=\nNEW_KEY=value\n", encoding="utf-8")
    subprocess.run([sys.executable, "scripts/generate-env.py", "--template", str(template), "--output", str(output)], check=True)
    text = output.read_text(encoding="utf-8")
    assert "ADMIN_PASSWORD=replace" not in text
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
    template.write_text(template.read_text(encoding="utf-8") + "LATER_KEY=yes\n", encoding="utf-8")
    subprocess.run([sys.executable, "scripts/upgrade-config.py", "--template", str(template), "--config", str(output)], check=True)
    assert "LATER_KEY=yes" in output.read_text(encoding="utf-8")
    subprocess.run([sys.executable, "scripts/set-env.py", str(output), "LATER_KEY", "changed"], check=True)
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0


def test_maintenance_dry_run_and_cleanup(tmp_path):
    repository = MemoryRepository()
    job = sample_job()
    job.status = JobStatus.FAILED
    repository.save_job(job)
    from core.pipeline import JobStore
    store = JobStore(tmp_path, repository=repository)
    report = cleanup_jobs(store, retention_days=1, dry_run=True)
    assert report["jobs"] == [job.job_id]
    assert job.job_id in store.jobs
    temporary = tmp_path / "stale.tmp"
    temporary.write_text("temporary", encoding="utf-8")
    os.utime(temporary, (time.time() - 3600, time.time() - 3600))
    assert str(temporary) in cleanup_temporary_files(tmp_path, older_than_hours=0, dry_run=False)
    assert not temporary.exists()
