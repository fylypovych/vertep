"""LAB installs on the same Ubuntu hosts as Vertep: 24.04 and 26.04 (0.0.3.60).

bootstrap-lab.sh and the R1 qualification only accepted 24.04, so the shared bootstrap
handed a 26.04 VM to the LAB installer, which refused it (observed 16.11.2026).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAB_BOOTSTRAP = (ROOT / "bootstrap-lab.sh").read_text(encoding="utf-8")
GUARD = re.compile(r'\[\[ \$\{ID:-\} == ubuntu && .*?\n  \|\| fail "Ubuntu Server 24\.04 or 26\.04 LTS is required"', re.S)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("os_id,version,accepted", [
    ("ubuntu", "24.04", True), ("ubuntu", "26.04", True),
    ("ubuntu", "22.04", False), ("debian", "24.04", False),
])
def test_bootstrap_lab_accepts_the_vertep_hosts(os_id, version, accepted):
    match = GUARD.search(LAB_BOOTSTRAP)
    assert match, "the LAB host check is missing"
    script = ('fail(){ printf "VERTEP LAB: %s\\n" "$*" >&2; exit 1; }\n'
              f"ID={os_id}; VERSION_ID={version}\n" + match.group(0) + "\necho accepted\n")
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert (result.returncode == 0 and result.stdout.strip() == "accepted") == accepted, result.stderr


def test_the_lab_supports_the_same_hosts_as_vertep():
    vertep = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    for version in ("24.04", "26.04"):
        assert f"    {version})" in vertep  # select_host_platform
        assert f"== {version}" in LAB_BOOTSTRAP


@pytest.mark.parametrize("version,ok", [("24.04", True), ("26.04", True), ("22.04", False)])
def test_r1_qualification_accepts_both_hosts(version, ok):
    from lab import qualification_run as run
    assert run.EXPECTED_OS_VERSIONS == ("24.04", "26.04")
    assert version.startswith(run.EXPECTED_OS_VERSIONS) == ok
