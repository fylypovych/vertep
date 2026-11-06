"""i.0.0.4.24 download, i.0.0.4.25 workflow guard, i.0.0.4.26 release key trust."""
import importlib.util
import json
import shutil
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.update_trust import authorize_release_key, validate_root_metadata
from core.workflow import mode
from services import backup_service


def _generator():
    spec = importlib.util.spec_from_file_location("root_metadata_426", "scripts/generate-root-metadata.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_download_streams_every_part_without_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_service, "_backup_root", lambda: tmp_path)
    (tmp_path / "snap.vtbackup").write_bytes(b"one")
    (tmp_path / "snap.vtbackup.part0001").write_bytes(b"two")
    (tmp_path / "backup.key").write_bytes(b"secret-key")
    client = TestClient(backup_service.app)
    answer = client.get("/snapshots/snap/download")
    assert answer.status_code == 200 and answer.content == b"onetwo"
    assert "snap.vtbackup" in answer.headers["content-disposition"]
    assert client.get("/snapshots/missing/download").status_code == 404
    assert client.get("/snapshots/..%2Fbackup/download").status_code in {400, 404}


def test_only_an_administrator_may_download():
    source = Path("core/api/auth.py").read_text(encoding="utf-8")
    assert 'request.url.path.endswith("/download")' in source


def _engine(monkeypatch, jobs):
    runtime = SimpleNamespace(engine=SimpleNamespace(_lock=RLock(), _jobs=jobs))
    monkeypatch.setattr(mode, "dynamic_definitions_root", lambda: "/definitions")
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: runtime)


def test_return_to_static_is_refused_while_dynamic_jobs_run(monkeypatch, tmp_path):
    monkeypatch.setattr(mode, "config_root", lambda: tmp_path)
    _engine(monkeypatch, {"a": {"status": "IN_PROGRESS"}, "b": {"status": "COMPLETED"},
                          "c": {"status": "IN_PROGRESS", "execution_mode": "legacy"}})
    with pytest.raises(ValueError, match="1"):
        mode.set_workflow_mode(mode.STATIC)


def test_return_to_static_is_allowed_when_nothing_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(mode, "config_root", lambda: tmp_path)
    _engine(monkeypatch, {"b": {"status": "COMPLETED"}})
    mode.set_workflow_mode(mode.STATIC)
    assert json.loads((tmp_path / mode._FILE).read_text())["mode"] == mode.STATIC


def test_keys_inside_a_repository_are_refused(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside the repository"):
        _generator()._refuse_keys_inside_a_repository(tmp_path / "installer" / "root-keys")


def test_no_private_key_is_tracked_in_root_keys():
    for path in Path("installer").rglob("*.pem"):
        assert b"PRIVATE KEY" not in path.read_bytes(), path


def test_metadata_authorizes_the_release_key_by_its_public_part(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is required")
    generator = _generator()
    private, public, keyring = tmp_path / "private", tmp_path / "public", tmp_path / "keyring"
    private.mkdir()
    public.mkdir()
    keyring.mkdir()
    release = Path("installer/update-public.pem")
    shutil.copy(release, keyring / "release-1.pem")
    for key_id in ("root-1", "root-2"):
        generator.generate_key(key_id, private)
        import subprocess
        (public / f"{key_id}.pem").write_bytes(subprocess.run(
            ["openssl", "pkey", "-in", str(private / f"{key_id}.pem"), "-pubout"],
            capture_output=True, check=True).stdout)
    metadata = generator.build_metadata(["root-1", "root-2"], private, 2, 365, ["stable"],
                                        release_public_keys={"release-1": release})
    validated = validate_root_metadata(metadata, public)
    assert authorize_release_key({"key_id": "release-1"}, validated, keyring, "stable") == keyring / "release-1.pem"
    with pytest.raises(RuntimeError):
        authorize_release_key({"key_id": "unknown"}, validated, keyring, "stable")


def test_release_key_must_be_public(tmp_path):
    secret = tmp_path / "secret.pem"
    secret.write_text("-----BEGIN PRIVATE KEY-----\n")
    generator = _generator()
    generator.generate_key("root-1", tmp_path)
    with pytest.raises(ValueError, match="public"):
        generator.build_metadata(["root-1"], tmp_path, 1, 365, ["stable"], release_public_keys={"release-1": secret})


def test_update_manifest_names_its_key():
    source = Path("scripts/build-update-manifest.py").read_text(encoding="utf-8")
    assert '"key_id": args.key_id' in source
