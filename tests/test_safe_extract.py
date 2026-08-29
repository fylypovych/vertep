import importlib.util
import io
import tarfile
from pathlib import Path

import pytest


def load_module():
    spec = importlib.util.spec_from_file_location("safe_extract", Path("scripts/safe-extract.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_with(tmp_path, member):
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.addfile(member, io.BytesIO(b"data") if member.isfile() else None)
    return archive


def test_extracts_regular_files(tmp_path):
    module = load_module()
    member = tarfile.TarInfo("release/VERSION")
    member.size = 4
    module.safe_extract(archive_with(tmp_path, member), tmp_path / "out", 100)
    assert (tmp_path / "out/release/VERSION").read_bytes() == b"data"


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("release/link", "link")])
def test_rejects_traversal_and_links(tmp_path, name, kind):
    module = load_module()
    member = tarfile.TarInfo(name)
    if kind == "link":
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
    else:
        member.size = 4
    with pytest.raises(ValueError, match="Unsafe"):
        module.safe_extract(archive_with(tmp_path, member), tmp_path / "out", 100)
