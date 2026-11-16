"""i.0.0.4.88 (#488): backup commands without shell=True for the built-in defaults.

The built-in pg_dump / pg_restore / redis-cli commands run as argument lists; the
Redis password travels in ``REDISCLI_AUTH``, never in argv. Operator overrides
(``BACKUP_*_CMD``) stay shell strings under the operator's responsibility, and the
snapshot path is substituted into ``BACKUP_REMOTE_CMD`` shell-quoted.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from services import backup_service

PASSWORD = "p@ss w0rd;$(touch /tmp/x)'\""


class Recorder:
    def __init__(self, produce=None):
        self.calls = []
        self.produce = produce or (lambda command: None)

    def __call__(self, command, shell=False, timeout=None, capture_output=True, env=None):
        self.calls.append({"command": command, "shell": shell, "env": env})
        self.produce(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")


def test_built_in_postgres_commands_are_argument_lists(monkeypatch, tmp_path):
    for name in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_DB"):
        monkeypatch.delenv(name, raising=False)
    dump = tmp_path / "dir with space;$(x)" / "vertep.dump"
    monkeypatch.setenv("BACKUP_PG_DUMP_PATH", str(dump))
    assert backup_service._pg_dump_default() == [
        "pg_dump", "-h", "postgres", "-p", "5432", "-U", "vertep", "-d", "vertep", "-Fc", "-f", str(dump)]
    monkeypatch.setenv("POSTGRES_HOST", "db.local")
    assert backup_service._pg_restore_default(dump) == [
        "pg_restore", "-h", "db.local", "-p", "5432", "-U", "vertep", "-d", "vertep",
        "-c", "--if-exists", str(dump)]


@pytest.mark.parametrize("url,host,port,database", [
    (f"redis://:{quote(PASSWORD, safe='')}@redis:6380/2",
     "redis", "6380", "2"),
    ("redis://:secret@cache/", "cache", "6379", "0"),
])
def test_the_redis_password_is_never_in_argv(monkeypatch, url, host, port, database):
    monkeypatch.setenv("REDIS_URL", url)
    argv, env = backup_service._redis_dump_default()
    assert argv == ["redis-cli", "-h", host, "-p", port, "-n", database, "BGSAVE"]
    assert env["REDISCLI_AUTH"] and all(env["REDISCLI_AUTH"] not in item for item in argv)
    if "6380" in url:
        assert env["REDISCLI_AUTH"] == PASSWORD


def test_redis_without_url_uses_redis_password(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_PASSWORD", "from-env")
    argv, env = backup_service._redis_dump_default()
    assert "from-env" not in " ".join(argv) and env == {"REDISCLI_AUTH": "from-env"}


def test_snapshot_runs_the_defaults_without_a_shell(monkeypatch, tmp_path):
    dump, rdb = tmp_path / "pg dump;x", tmp_path / "redis dump.rdb"
    monkeypatch.setenv("BACKUP_PG_DUMP_PATH", str(dump))
    monkeypatch.setenv("BACKUP_REDIS_DUMP_PATH", str(rdb))
    monkeypatch.delenv("BACKUP_PG_DUMP_CMD", raising=False)
    monkeypatch.delenv("BACKUP_REDIS_DUMP_CMD", raising=False)
    monkeypatch.delenv("BACKUP_REDIS_LOGICAL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://:hidden-secret@redis:6379/0")
    monkeypatch.setattr(backup_service, "_sources", lambda: [])

    def produce(command):
        (dump if command[0] == "pg_dump" else rdb).write_bytes(b"x")
    recorder = Recorder(produce)
    monkeypatch.setattr(backup_service.subprocess, "run", recorder)
    backup_service._archive(tmp_path / "snapshot.tar.gz")
    assert [call["command"][0] for call in recorder.calls] == ["pg_dump", "redis-cli"]
    assert all(call["shell"] is False for call in recorder.calls)
    assert recorder.calls[1]["env"]["REDISCLI_AUTH"] == "hidden-secret"
    assert "hidden-secret" not in repr(recorder.calls[1]["command"])


def test_empty_overrides_still_skip_the_dumps(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_REDIS_LOGICAL", raising=False)
    monkeypatch.setattr(backup_service, "_sources", lambda: [])
    recorder = Recorder()
    monkeypatch.setattr(backup_service.subprocess, "run", recorder)
    backup_service._archive(tmp_path / "snapshot.tar.gz")
    assert recorder.calls == []


@pytest.mark.skipif(shutil.which("sh") is None, reason="a POSIX shell is required")
def test_remote_copy_quotes_the_snapshot_path(monkeypatch, tmp_path):
    """A real shell run: a path with spaces and metacharacters arrives as one argument."""
    snapshot = tmp_path / "snap shot;$(touch pwned)'q.tar.gz"
    snapshot.write_bytes(b"data")
    received = tmp_path / "received.txt"
    script = tmp_path / "receive.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[2]).write_text(sys.argv[1])\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BACKUP_REMOTE_CMD", f"{sys.executable} {script} {{file}} {received}")
    assert backup_service._remote_copy(snapshot) is True
    assert received.read_text() == str(snapshot)
    assert not (tmp_path / "pwned").exists()


def test_operator_overrides_remain_shell_strings(monkeypatch, tmp_path):
    marker = tmp_path / "dump"
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", f"echo ok > {marker}")
    monkeypatch.setenv("BACKUP_PG_DUMP_PATH", str(marker))
    monkeypatch.setenv("BACKUP_REDIS_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_REDIS_LOGICAL", raising=False)
    monkeypatch.setattr(backup_service, "_sources", lambda: [])
    backup_service._archive(tmp_path / "snapshot.tar.gz")  # the shell redirect created the dump
    assert not marker.exists()  # added to the archive, then removed as before


def test_no_built_in_command_uses_a_shell():
    source = Path(backup_service.__file__).read_text(encoding="utf-8")
    assert source.count("shell=True") == 1  # only _run_command, for operator strings
    assert "${POSTGRES_HOST" not in source and "redis-cli -u" not in source


@pytest.mark.parametrize("scheme,username,expected_options", [
    ("rediss", "backup%20user", ["--tls", "--user", "backup user"]),
    ("rediss", "", ["--tls"]),
    ("redis", "backup%40user", ["--user", "backup@user"]),
    ("redis", "", []),
])
def test_redis_url_transport_and_acl_reach_subprocess(monkeypatch, tmp_path,
                                                    scheme, username, expected_options):
    """#490: exercise the archive path, including the subprocess argv/env boundary."""
    url = f"{scheme}://{username}:{quote(PASSWORD, safe='')}@cache.example:6380/2"
    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("BACKUP_PG_DUMP_CMD", "")
    monkeypatch.delenv("BACKUP_REDIS_DUMP_CMD", raising=False)
    monkeypatch.delenv("BACKUP_REDIS_LOGICAL", raising=False)
    rdb = tmp_path / "redis dump.rdb"
    monkeypatch.setenv("BACKUP_REDIS_DUMP_PATH", str(rdb))
    monkeypatch.setattr(backup_service, "_sources", lambda: [])
    recorder = Recorder(lambda command: rdb.write_bytes(b"redis fixture"))
    monkeypatch.setattr(backup_service.subprocess, "run", recorder)

    backup_service._archive(tmp_path / "snapshot.tar.gz")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["command"] == ["redis-cli", "-h", "cache.example", "-p", "6380",
                               "-n", "2", *expected_options, "BGSAVE"]
    assert call["shell"] is False
    assert call["env"]["REDISCLI_AUTH"] == PASSWORD
    assert all(PASSWORD not in arg and quote(PASSWORD, safe='') not in arg and url not in arg
               for arg in call["command"])
