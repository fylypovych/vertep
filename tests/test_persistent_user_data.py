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
