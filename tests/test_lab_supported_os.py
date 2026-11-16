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


def test_the_lab_install_needs_at_least_4_gb_of_ram():
    """Owner decision 16.11.2026: a VM with nominal 8 GB reports ~7.4 GB; the floor is 4096 MB."""
    assert "MIN_RAM_MB=${VERTEP_LAB_MIN_RAM_MB:-4096}" in LAB_BOOTSTRAP


@pytest.mark.parametrize("gib,ok", [(3.9, False), (4.0, True), (7.25, True), (8.0, True), (16.0, False)])
def test_r1_ram_range_is_4_to_8_gb(gib, ok):
    """Owner decision 16.11.2026: R1 RAM 4–8 GB (was 16–32); a nominal 8 GB VM reports ~7.25 GiB."""
    from lab import qualification_run as run
    low, high = run.EXPECTED_RAM_GB
    assert (low, high) == (4.0, 8.0) and (low <= gib <= high) == ok


# ── LAB VM 16.11.2026: lab-web crash-looped with nginx.conf "Permission denied" ──

CHECKOUT = re.compile(r'if \[\[ -d \$LAB_ROOT/src/\.git \]\]; then\n.*?\nchmod -R go\+rX "\$LAB_ROOT/src"\n', re.S)


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="bash and git are required")
@pytest.mark.parametrize("existing", [False, True])
def test_the_checkout_is_readable_by_unprivileged_image_users(tmp_path, existing):
    import os
    import stat
    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-C", str(origin), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    (origin / "nginx.conf").write_text("events {}\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    if existing:  # a checkout made by the old bootstrap with umask 077
        subprocess.run(["bash", "-c", f"umask 077; git clone -q {origin} {root}/src"], check=True)
        assert stat.S_IMODE(os.stat(root / "src/nginx.conf").st_mode) == 0o600
    script = (f"set -e; umask 077; LAB_ROOT={root}; REF=main; REPOSITORY={origin}\n"
              + CHECKOUT.search(LAB_BOOTSTRAP).group(0)
              + f'touch {root}/secret; stat -c %a {root}/secret\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(os.stat(root / "src/nginx.conf").st_mode) & 0o044 == 0o044
    assert result.stdout.strip() == "600"  # later files (lab.env secrets) keep umask 077


def test_lab_images_set_readable_modes_explicitly():
    web = (ROOT / "deploy/lab/Dockerfile.web").read_text(encoding="utf-8")
    sandbox = (ROOT / "deploy/lab/Dockerfile.sandbox").read_text(encoding="utf-8")
    assert "COPY --chmod=0644 deploy/lab/web/nginx.conf /etc/nginx/nginx.conf" in web
    assert "chmod -R a+rX /srv/lab-web" in web
    assert "chmod -R a+rX $VERTEP_LAB_HOME/lab" in sandbox


FINISH = re.compile(r"web_port=\$\(sed -n 's/\^LAB_WEB_PORT=//p'.*?\nprintf 'і відкрийте http://127\.0\.0\.1:%s\\n' \"\$web_port\"\n", re.S)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("env_port,expected_port", [("", "8444"), ("LAB_WEB_PORT=9555\n", "9555")])
def test_the_finish_message_names_the_real_address(tmp_path, env_port, expected_port):
    env_file = tmp_path / "lab.env"
    env_file.write_text("LAB_OWNER_TOKEN=x\n" + env_port, encoding="utf-8")
    script = (f"env_file={env_file}; SUDO_USER=lab\n"
              "hostname(){ echo '192.0.2.10 10.0.0.5'; }\n" + FINISH.search(LAB_BOOTSTRAP).group(0))
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert f"ssh -L {expected_port}:127.0.0.1:{expected_port} lab@192.0.2.10" in result.stdout
    assert f"http://127.0.0.1:{expected_port}" in result.stdout and "<" not in result.stdout
