"""Source identity attached to #356/#357 JUnit evidence, including local edits."""
import hashlib
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def acceptance_provenance(record_property, request):
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)
    record_property("source_sha", git("rev-parse", "HEAD").decode().strip())
    record_property("tracked_diff_sha256", hashlib.sha256(git("diff", "HEAD")).hexdigest())
    record_property("test_sha256", hashlib.sha256(Path(str(request.fspath)).read_bytes()).hexdigest())
    record_property("source_status", "dirty" if git("status", "--porcelain").strip() else "clean")
