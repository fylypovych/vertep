"""Disposable PostgreSQL for module data tests.

Order: ``VERTEP_TEST_DATABASE_URL`` (fresh database created inside it), local
PostgreSQL binaries, then a throwaway ``postgres:16-alpine`` container.
``VERTEP_REQUIRE_MODULE_POSTGRES=1`` turns unavailability into a failure.
"""

import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait(url: str, timeout: float = 90) -> None:
    import psycopg
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
                return
        except psycopg.Error:
            time.sleep(0.5)
    pytest.fail("Disposable PostgreSQL did not become ready")


def _unavailable(message: str):
    if os.getenv("VERTEP_REQUIRE_MODULE_POSTGRES") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture
def module_postgres(tmp_path):
    import psycopg
    external = os.getenv("VERTEP_TEST_DATABASE_URL")
    if external:
        name = "vertep_mod_" + uuid.uuid4().hex[:10]
        with psycopg.connect(external, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{name}"')
        base = external.rsplit("/", 1)[0]
        yield f"{base}/{name}"
        with psycopg.connect(external, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        return
    pg_bin = Path(os.getenv("VERTEP_TEST_POSTGRES_BIN", "/usr/lib/postgresql/16/bin"))
    if (pg_bin / "initdb").is_file():
        data, sockets = tmp_path / "pg", tmp_path / "sock"
        sockets.mkdir()
        subprocess.run([str(pg_bin / "initdb"), "-D", str(data), "-U", "vertep", "-A", "trust", "--encoding=UTF8", "--no-locale"],
                       check=True, capture_output=True)
        port = _port()
        process = subprocess.Popen([str(pg_bin / "postgres"), "-D", str(data), "-h", "127.0.0.1", "-p", str(port),
                                    "-k", str(sockets)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"postgresql://vertep@127.0.0.1:{port}/postgres"
        try:
            _wait(url)
            yield url
        finally:
            # SIGTERM is PostgreSQL's smart shutdown: it waits for every client, and a module
            # service in the test process keeps its connection. SIGINT disconnects them (fast).
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
        return
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
        _unavailable("Disposable PostgreSQL is unavailable (no URL, binaries or Docker)")
    for attempt in range(3):
        # The host port is chosen on this side; Docker Desktop may still hold it.
        name, port = "vertep-test-pg-" + uuid.uuid4().hex[:8], _port()
        started = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test",
                                  "-e", "POSTGRES_USER=vertep", "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine"],
                                 capture_output=True, text=True, timeout=300)
        if started.returncode == 0:
            break
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
    else:
        pytest.fail(f"Disposable PostgreSQL container did not start: {started.stderr.strip()}")
    url = f"postgresql://vertep:test@127.0.0.1:{port}/postgres"
    try:
        _wait(url)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
