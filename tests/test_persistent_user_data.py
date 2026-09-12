import json
from pathlib import Path
import core.persistent_data as pd

def test_ensure_persistent_user_data_seeds_once_and_does_not_recreate_deleted(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    seed = tmp_path / "seed"
    # prepare seed payloads
    for rel in ("characters", "brands", "workflows"):
        src = seed / rel
        (src / "image" if rel == "workflows" else src / "did_test").mkdir(parents=True)
        if rel == "workflows":
            (src / "image" / "demo.json").write_text('{"a":1}', encoding="utf-8")
            (src / "video" / "demo.json").parent.mkdir(parents=True, exist_ok=True)
            (src / "video" / "demo.json").write_text('{"v":1}', encoding="utf-8")
        elif rel == "characters":
            (src / "did_test" / "character.json").write_text(json.dumps({"id":"did_test","name":"T"}), encoding="utf-8")
        else:
            (src / "brand01" / "brand.json").parent.mkdir(parents=True, exist_ok=True)
            (src / "brand01" / "brand.json").write_text(json.dumps({"id":"brand01","name":"B"}), encoding="utf-8")

    monkeypatch.setenv("JOB_ROOT", str(storage / "jobs"))
    monkeypatch.setenv("CHARACTERS_ROOT", str(storage / "characters"))
    monkeypatch.setenv("BRANDS_ROOT", str(storage / "brands"))
    monkeypatch.setenv("WORKFLOWS_ROOT", str(storage / "workflows"))
    # Point seed fallback to our seed via env override keys used by persistent_data
    monkeypatch.setenv("CHARACTERS_SEED_ROOT", str(seed / "characters"))
    monkeypatch.setenv("BRANDS_SEED_ROOT", str(seed / "brands"))
    monkeypatch.setenv("WORKFLOWS_SEED_ROOT", str(seed / "workflows"))

    res1 = pd.ensure_persistent_user_data()
    assert (storage / "characters" / "did_test" / "character.json").exists()
    assert (storage / "workflows" / "image" / "demo.json").exists()
    assert (storage / "brands" / "brand01" / "brand.json").exists()
    assert res1["seed_skipped"] is False

    # Simulate user deletion: remove a character directory
    import shutil
    shutil.rmtree(storage / "characters" / "did_test")
    # Second ensure must NOT recreate deleted entry (seed marker prevents it)
    res2 = pd.ensure_persistent_user_data()
    assert not (storage / "characters" / "did_test").exists()
    assert res2["seed_skipped"] is True

def test_ensure_persistent_user_data_migrates_legacy_without_overwrite(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    legacy = tmp_path / "legacy"
    # legacy has a workflow and a character with different content
    legacy_char = legacy / "characters" / "did_legacy" / "character.json"
    legacy_char.parent.mkdir(parents=True)
    legacy_char.write_text(json.dumps({"id":"did_legacy","name":"Legacy"}), encoding="utf-8")
    legacy_wf = legacy / "workflows" / "image" / "demo.json"
    legacy_wf.parent.mkdir(parents=True)
    legacy_wf.write_text('{"legacy":true}', encoding="utf-8")

    # Persistent already has a different version of same file — should NOT be overwritten
    persistent_wf = storage / "workflows" / "image" / "demo.json"
    persistent_wf.parent.mkdir(parents=True)
    persistent_wf.write_text('{"persistent":true}', encoding="utf-8")

    # Emulate legacy at /app/<name> by monkeypatching _copy path: we place legacy at Path("/app/...") is not writable,
    # so instead test the low-level helper via direct call of _copy_missing in migration loop:
    # Instead, create persistent structure then manually copy legacy via helper.
    monkeypatch.setenv("JOB_ROOT", str(storage / "jobs"))
    monkeypatch.setenv("CHARACTERS_ROOT", str(storage / "characters"))
    monkeypatch.setenv("BRANDS_ROOT", str(storage / "brands"))
    monkeypatch.setenv("WORKFLOWS_ROOT", str(storage / "workflows"))

    # Call helper directly to simulate migration of an arbitrary legacy dir
    # The ensure function migrates only from /app/<name> and ./<name>, so we emulate by calling _copy_missing.
    copied = pd._copy_missing(legacy / "characters", storage / "characters", overwrite=False)
    assert copied >= 1
    assert (storage / "characters" / "did_legacy" / "character.json").exists()

    copied2 = pd._copy_missing(legacy / "workflows", storage / "workflows", overwrite=False)
    # demo.json already exists -> should not overwrite
    assert persistent_wf.read_text(encoding="utf-8") == '{"persistent":true}'


def test_persistent_api_resources_survive_recreate(monkeypatch, tmp_path):
    """Acceptance excerpt for #32: CRUD via API persists after simulated recreate.
    Creates character/brand/workflow + job then verifies they remain after a
    second process would have re-run ensure_persistent_user_data (marker prevents re-seed overwrite).
    """
    from fastapi.testclient import TestClient
    from core.app import app
    storage_char = tmp_path / "storage" / "characters"
    storage_brand = tmp_path / "storage" / "brands"
    storage_wf = tmp_path / "storage" / "workflows"
    storage_jobs = tmp_path / "storage" / "jobs"
    for p in (storage_char, storage_brand, storage_wf, storage_jobs):
        p.mkdir(parents=True, exist_ok=True)
    # minimal seed file so storage is not empty
    monkeypatch.setenv("JOB_ROOT", str(storage_jobs))
    monkeypatch.setenv("CHARACTERS_ROOT", str(storage_char))
    monkeypatch.setenv("BRANDS_ROOT", str(storage_brand))
    monkeypatch.setenv("WORKFLOWS_ROOT", str(storage_wf))
    # Ensure persistent init marks storage as initialized
    import core.app as core_app
    import core.state
    monkeypatch.setattr(core.state.workflow_registry, "root", storage_wf)
    monkeypatch.setattr(core_app.workflow_registry, "root", storage_wf)
    (storage_wf / "image").mkdir(parents=True, exist_ok=True)
    (storage_wf / "image" / "demo.json").write_text('{"1": {"class_type": "LoadImage", "inputs": {"image": "demo.png"}}}', encoding="utf-8")
    pd.ensure_persistent_user_data()
    client = TestClient(app)
    # Create character via API
    char = {"id": "testchar", "name": "Test Char", "language": "uk", "enabled": True, "system_prompt": "hi", "voice": {}, "visual": {}, "generation": {}, "publishing": {}}
    r = client.put("/api/characters/testchar", json=char)
    assert r.status_code in (200, 201)
    # Create brand via API (brands uses brand.json file)
    brand = {"id": "testbrand", "name": "Test Brand", "enabled": True}
    r = client.put("/api/brands/testbrand", json=brand)
    assert r.status_code in (200, 201)
    # Create workflow via API
    wf_content = {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}}}
    r = client.put("/api/workflows/image/test_persist_abc.json", json=wf_content)
    assert r.status_code == 200
    # Create job via API — will validate character exists
    r = client.post("/api/jobs", json={"topic": "Persist test", "character_id": "testchar"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    # Simulate container recreate: re-run ensure (should NOT wipe)
    pd.ensure_persistent_user_data()
    # Verify still present
    assert client.get("/api/characters/testchar").status_code == 200
    assert client.get("/api/brands").json() and any(b["id"] == "testbrand" for b in client.get("/api/brands").json())
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    # Verify deletion survives recreate — delete after removing job reference
    # Character is still referenced by the earlier job, so remove job first
    client.delete(f"/api/jobs/{job_id}")
    r = client.delete("/api/characters/testchar")
    assert r.status_code == 200
    pd.ensure_persistent_user_data()
    assert client.get("/api/characters/testchar").status_code == 404


def test_backup_covers_storage_and_restore(monkeypatch, tmp_path):
    """Backup Service covers /data/storage (characters/brands/workflows/jobs) and restores."""
    import base64
    from fastapi.testclient import TestClient
    from services import backup_service
    config = tmp_path / "config"
    storage = tmp_path / "storage"
    backups = tmp_path / "backups"
    config.mkdir(); storage.mkdir(); backups.mkdir()
    (storage / "characters" / "c1").mkdir(parents=True)
    (storage / "characters" / "c1" / "character.json").write_text('{"id":"c1"}', encoding="utf-8")
    (storage / "workflows" / "image").mkdir(parents=True)
    (storage / "workflows" / "image" / "demo.json").write_text('{"x":1}', encoding="utf-8")
    (storage / "jobs" / "job-1").mkdir(parents=True)
    (storage / "jobs" / "job-1" / "job.json").write_text('{"job_id":"job-1"}', encoding="utf-8")
    monkeypatch.setenv("BACKUP_CONFIG_ROOT", str(config))
    monkeypatch.setenv("BACKUP_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.b64encode(b"k"*32).decode())
    client = TestClient(backup_service.app)
    resp = client.post("/snapshots", json={"job_id": "j1", "request": {}})
    assert resp.status_code == 200
    snap_id = resp.json()["snapshot_id"]
    # Wipe storage
    import shutil
    shutil.rmtree(storage / "characters" / "c1")
    (storage / "workflows" / "image" / "demo.json").unlink()
    assert not (storage / "characters" / "c1").exists()
    # Restore
    resp = client.post(f"/snapshots/{snap_id}/restore")
    assert resp.status_code == 200
    assert (storage / "characters" / "c1" / "character.json").exists()
    assert (storage / "workflows" / "image" / "demo.json").exists()
