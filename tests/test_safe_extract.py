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


def make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except Exception as error:
        pytest.skip(f"directory link creation unavailable: {error}")


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


@pytest.mark.parametrize("name,kind", [
    ("/etc/passwd", "file"),
    ("release/hard", "hardlink"),
    ("release/chr", "chr"),
    ("release/fifo", "fifo"),
])
def test_rejects_absolute_links_and_special_files(tmp_path, name, kind):
    module = load_module()
    member = tarfile.TarInfo(name)
    if kind == "file":
        member.size = 4
    elif kind == "hardlink":
        member.type = tarfile.LNKTYPE
        member.linkname = "release/VERSION"
    elif kind == "chr":
        member.type = tarfile.CHRTYPE
    else:
        member.type = tarfile.FIFOTYPE
    with pytest.raises(ValueError, match="Unsafe"):
        module.safe_extract(archive_with(tmp_path, member), tmp_path / "out", 100)


def test_rejects_duplicate_entries(tmp_path):
    module = load_module()
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for _ in range(2):
            member = tarfile.TarInfo("release/VERSION")
            member.size = 4
            output.addfile(member, io.BytesIO(b"data"))
    with pytest.raises(ValueError, match="Unsafe"):
        module.safe_extract(archive, tmp_path / "out", 100)


def test_rejects_archive_over_size_limit(tmp_path):
    module = load_module()
    member = tarfile.TarInfo("release/VERSION")
    member.size = 4
    with pytest.raises(ValueError, match="size limit"):
        module.safe_extract(archive_with(tmp_path, member), tmp_path / "out", 3)


def test_rejects_pre_existing_symlink_parent(tmp_path):
    module = load_module()
    output = tmp_path / "out"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_directory_link(output / "release", outside)
    member = tarfile.TarInfo("release/VERSION")
    member.size = 4
    with pytest.raises(ValueError, match="Unsafe"):
        module.safe_extract(archive_with(tmp_path, member), output, 100)
    assert not (outside / "VERSION").exists()


def test_rejects_pre_existing_symlink_on_directory_member(tmp_path):
    module = load_module()
    output = tmp_path / "out"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_directory_link(output / "release", outside)
    member = tarfile.TarInfo("release/")
    member.type = tarfile.DIRTYPE
    with pytest.raises(ValueError, match="Unsafe"):
        module.safe_extract(archive_with(tmp_path, member), output, 100)
