"""#524: `vertep remove` — short summary, backup choice, complete removal and a copy that the
standard admin-panel restore imports after a clean reinstall. Disposable root, simulated OS/Docker,
the real Backup Service code and a disposable PostgreSQL; never the host installation."""
import contextlib
import copy
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import removal as r
from services import backup_service
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)
from tests.test_removal import Runtime, _as_linux_root

KEY = "ab" * 32
PG_SCRIPT = '''
import sys, psycopg
action, url, path = sys.argv[1:4]
with psycopg.connect(url, autocommit=True) as db:
    if action == "dump" and db.execute("SELECT to_regclass('items')").fetchone()[0] is None:
        open(path, "wb").close()  # an empty database dumps to an empty copy
    elif action == "dump":
        with open(path, "wb") as out, db.cursor().copy("COPY items TO STDOUT") as copy:
            for chunk in copy:
                out.write(chunk)
    else:
        db.execute("CREATE TABLE IF NOT EXISTS items (name text)")
        db.execute("TRUNCATE items")
        with open(path, "rb") as source, db.cursor().copy("COPY items FROM STDIN") as copy:
            copy.write(source.read())
'''


class BackupRuntime(Runtime):
    """Simulated Docker whose `docker exec … removal-snapshot` runs the real Backup Service code."""
    def __init__(self, root):
        super().__init__(root)
        service = copy.deepcopy(self.containers["own"])
        service["Id"] = "backup"
        service["Config"]["Labels"]["com.docker.compose.service"] = "backup-service"
        service["Mounts"] = []
        self.containers["backup"] = service
        self.networks["private"]["Containers"]["backup"] = {}
        self.snapshot = None

    def run(self, args, **kwargs):
        if args[:3] == ["docker", "exec", "backup"] and "removal-snapshot" in args:
            self.calls.append(args)
            if self.snapshot is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="pg_dump failed")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.snapshot(args[args.index("removal-snapshot"):])
            return SimpleNamespace(returncode=0, stdout=output.getvalue(), stderr="")
        return super().run(args, **kwargs)


def _layout(root):
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/vertep").write_text("runtime")
    (root / "config/module-lifecycle/state").mkdir(parents=True)
    (root / "config/backup.key").write_text(KEY)
    (root / "config/brand.json").write_text('{"brand": "old"}')
    (root / "config/module-lifecycle/state/brands.json").write_text(
        json.dumps({"module_id": "brands", "version": "0.0.4.20", "state": "active"}))
    (root / "storage").mkdir()
    (root / "storage/video.mp4").write_bytes(b"old video")
    (root / "backups").mkdir()
    (root / "VERSION").write_text("0.0.5.30\n")
    (root / "docker-compose.yml").write_text("services: {}")
    (root / "manifest.json").write_text(json.dumps({"files": {"scripts/vertep": {}, "docker-compose.yml": {}}}))


@pytest.fixture
def install(tmp_path, monkeypatch):
    root = tmp_path / "srv/vertep"
    _layout(root)
    runtime = BackupRuntime(root)
    host = r.Host(root, runtime.run, tmp_path / "system")
    containers, images = dict(runtime.containers), set(runtime.images)
    runtime.containers = {"foreign": containers["foreign"]}
    runtime.images = {"sha256:foreign"}
    r.record(host)  # bootstrap before the first pull
    runtime.containers, runtime.images = containers, images
    r.record(host)
    runtime.calls.clear()
    monkeypatch.setattr(r, "JOURNAL_BASE", tmp_path / "journals")
    _as_linux_root(monkeypatch, host)
    return host, runtime


def _backup_env(monkeypatch, root, database_url=None, scripts=None):
    for name, value in {"BACKUP_ROOT": root / "backups", "BACKUP_CONFIG_ROOT": root / "config",
                        "BACKUP_STORAGE_ROOT": root / "storage",
                        "BACKUP_ENCRYPTION_KEY_FILE": root / "config/backup.key",
                        "BACKUP_PG_DUMP_PATH": root / "backups/.vertep-postgres.dump"}.items():
        monkeypatch.setenv(name, str(value))
    for name in ("BACKUP_ENCRYPTION_KEY", "BACKUP_SOURCES", "BACKUP_CORE_URL", "CORE_ADDRESS",
                 "BACKUP_PG_RESTORE_CMD", "BACKUP_REDIS_LOGICAL", "BACKUP_REMOTE_CMD", "BACKUP_RESTORE_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    if database_url:
        monkeypatch.delenv("BACKUP_PG_DUMP_CMD", raising=False)
        monkeypatch.setattr(backup_service, "_pg_dump_default", lambda: [
            sys.executable, str(scripts), "dump", database_url, backup_service._pg_dump_path()])
        monkeypatch.setattr(backup_service, "_pg_restore_default", lambda dump: [
            sys.executable, str(scripts), "restore", database_url, str(dump)])
    else:
        monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")


def _main(args, capsys):
    code = r.main(args)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_summary_shows_components_and_backup_choice_and_no_stops_without_changes(install, monkeypatch, capsys):
    host, runtime = install
    monkeypatch.setattr(r.sys.stdin, "isatty", lambda: True)
    answers = iter(["db", "NO"])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))
    code, output = _main(["--root", str(host.root)], capsys)
    assert code == 1 and "не підтверджене" in output
    assert "CORE 0.0.5.30" in output and "модуль brands 0.0.4.20 (active)" in output
    assert "full" in output and "db" in output and "none" in output
    assert prompts == ["Оберіть копію [full/db/none]: ", "Підтвердити видалення за цим переліком? [YES/NO]: "]
    # The ordinary view lists neither files nor their hashes.
    assert "video.mp4" not in output and "sha256" not in output and "plan_sha256" not in output
    assert not any(call[:2] == ["docker", "exec"] for call in runtime.calls)
    assert not any("rm" in call or "disable" in call for call in runtime.calls)
    assert (host.root / "storage/video.mp4").exists()
    # --dry-run keeps the full inventory.
    code, output = _main(["--root", str(host.root), "--dry-run"], capsys)
    assert code == 0 and "storage/video.mp4" in output and "plan_sha256" in output


def test_non_interactive_removal_requires_an_explicit_backup_choice(install, capsys):
    host, runtime = install
    sha = r.plan(host, True)["plan_sha256"]
    code, output = _main(["--root", str(host.root), "--confirm", sha], capsys)
    assert code == 1 and "--backup full|db|none" in output
    assert (host.root / "scripts/vertep").exists() and "own" in runtime.containers


@pytest.mark.parametrize("failure", ["service-down", "snapshot-fails", "unverified"])
def test_failed_backup_does_not_start_the_removal(install, capsys, monkeypatch, failure):
    host, runtime = install
    if failure == "service-down":
        runtime.containers["backup"]["State"]["Running"] = False
    elif failure == "unverified":
        runtime.snapshot = lambda argv: print(json.dumps({"snapshot_id": "x", "verified": False}))
    sha = r.plan(host, True)["plan_sha256"]
    code, output = _main(["--root", str(host.root), "--backup", "full", "--confirm", sha], capsys)
    assert code == 1 and "видалення не починалося" in output
    assert (host.root / "storage/video.mp4").read_bytes() == b"old video"
    assert (host.root / "scripts/vertep").exists()
    assert {"own", "backup", "foreign"} <= set(runtime.containers) and runtime.volumes
    assert not (host.root / "config/removal-in-progress.json").exists()
    assert not any(call[:2] in (["systemctl", "stop"], ["docker", "rm"]) for call in runtime.calls)


def test_confirmed_removal_without_backup_removes_everything_and_keeps_foreign(install, capsys):
    host, runtime = install
    foreign = copy.deepcopy(runtime.containers["foreign"])
    runtime.volumes["shared"] = {"Name": "shared", "Labels": {}}  # not created by this installation
    sha = r.plan(host, True)["plan_sha256"]
    code, output = _main(["--root", str(host.root), "--backup", "none", "--confirm", sha], capsys)
    assert code == 0 and "SUCCEEDED" in output
    assert not host.root.exists()  # DB volume, data, config, modules and software are gone
    assert runtime.containers == {"foreign": foreign}
    assert runtime.images == {"sha256:foreign"} and set(runtime.volumes) == {"shared"}
    assert not any(call[:2] == ["docker", "exec"] for call in runtime.calls)


def test_partial_removal_reports_what_remained_and_the_journal(install, capsys):
    host, runtime = install
    (host.root / "operator-notes.txt").write_text("not Vertep")
    sha = r.plan(host, True)["plan_sha256"]
    code, output = _main(["--root", str(host.root), "--backup", "none", "--confirm", sha], capsys)
    assert code == 2 and "PARTIAL" in output and "state.json" in output
    assert "Лишилося" in output and str(host.root / "operator-notes.txt") in output
    assert (host.root / "operator-notes.txt").read_text() == "not Vertep"
    assert not (host.root / "config").exists() and not (host.root / "storage").exists()


@pytest.mark.parametrize("scope", ["full", "db"])
def test_backup_survives_removal_and_restores_after_clean_reinstall(install, capsys, monkeypatch, tmp_path,
                                                                    module_postgres, scope):
    import psycopg
    host, runtime = install
    root = host.root
    scripts = tmp_path / "pg_copy.py"
    scripts.write_text(PG_SCRIPT)
    with psycopg.connect(module_postgres, autocommit=True) as db:
        db.execute("CREATE TABLE items (name text)")
        db.execute("INSERT INTO items VALUES ('old job'), ('old brand')")
    _backup_env(monkeypatch, root, module_postgres, scripts)
    runtime.snapshot = backup_service._removal_snapshot
    before = json.loads((root / "config/removal-ownership.json").read_text())

    sha = r.plan(host, True)["plan_sha256"]
    code, output = _main(["--root", str(root), "--backup", scope, "--confirm", sha], capsys)
    assert code == 0 and "SUCCEEDED" in output, output
    copies = list((host.system / r.BACKUP_BASE).iterdir())
    assert len(copies) == 1 and str(copies[0]) in output
    saved = copies[0]
    assert not saved.is_relative_to(root) and not root.exists()
    assert (saved / "backup.key").read_text() == KEY
    receipt = json.loads(next(saved.glob("*.json")).read_text())
    assert receipt["scope"] == scope and receipt["format"] == "VERTEP-BACKUP-v1"

    # Removed database volume: the new installation starts with an empty database.
    with psycopg.connect(module_postgres, autocommit=True) as db:
        db.execute("DROP TABLE items")
    # Clean reinstall on the same root: no old state is picked up; the operator only
    # puts the saved key in place so the copy can be decrypted later.
    _layout(root)
    for path in [root / "config/brand.json", root / "storage/video.mp4", root / "config/module-lifecycle"]:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    (root / "storage/new.mp4").write_bytes(b"new video")
    shutil.copy2(saved / "backup.key", root / "config/backup.key")
    assert not (root / "config/installation.json").exists()  # the First Run Wizard is shown
    runtime.containers = {"foreign": runtime.containers["foreign"]}
    after = r.record(host)
    assert after["installation_id"] != before["installation_id"]

    # Standard admin-panel restore after copying the snapshot into the backups directory.
    for item in saved.iterdir():
        if item.name != "backup.key":
            shutil.copy2(item, root / "backups" / item.name)
    monkeypatch.setattr(backup_service, "_get_system_state", lambda: None)
    monkeypatch.setattr(backup_service, "_core_available", lambda: True)
    monkeypatch.setattr(backup_service, "_set_system_state", lambda *_, **__: None)
    client = TestClient(backup_service.app)
    listed = [item["snapshot_id"] for item in client.get("/snapshots").json()["snapshots"]]
    assert receipt["snapshot_id"] in listed
    token = client.post(f"/snapshots/{receipt['snapshot_id']}/restore").json()["confirmation_token"]
    response = client.post(f"/snapshots/{receipt['snapshot_id']}/restore/confirm", params={"token": token})
    assert response.status_code == 200, response.text

    with psycopg.connect(module_postgres) as db:  # read-back of the database in both modes
        assert sorted(row[0] for row in db.execute("SELECT name FROM items")) == ["old brand", "old job"]
    if scope == "full":
        assert (root / "storage/video.mp4").read_bytes() == b"old video"
        assert (root / "config/brand.json").read_text() == '{"brand": "old"}'
        assert not (root / "storage/new.mp4").exists()
    else:  # the file storage is not replaced by a database-only copy
        assert (root / "storage/new.mp4").read_bytes() == b"new video"
        assert not (root / "storage/video.mp4").exists() and not (root / "config/brand.json").exists()
    assert (root / "config/backup.key").read_text() == KEY
    # The restored copy never replaces this host's ownership inventory.
    assert json.loads((root / "config/removal-ownership.json").read_text())["installation_id"] == after["installation_id"]


def test_database_only_snapshot_carries_no_files_and_fails_without_a_dump(tmp_path, monkeypatch):
    root = tmp_path / "root"
    _layout(root)
    _backup_env(monkeypatch, root)  # dump disabled: neither copy can be verified
    for scope in ("full", "db"):
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            backup_service._removal_snapshot(["removal-snapshot", "--scope", scope])
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", f"echo dump > {root / 'backups/.vertep-postgres.dump'}")
    receipt = backup_service.verify_snapshot(backup_service._create_snapshot("t", False, "db")["snapshot_id"], "db")
    assert receipt["inventory"] == [] and receipt["verified"]
    with pytest.raises(RuntimeError, match="Receipt"):
        backup_service.verify_snapshot(receipt["snapshot_id"], "full")
