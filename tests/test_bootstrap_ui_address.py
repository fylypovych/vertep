"""#507: the address bootstrap prints after install/update.

Runs the real bootstrap functions (extracted between their markers) in bash with
stubbed ``ip``/``hostname`` so multi-NIC, single-NIC, IPv6 and configured
addresses are exercised without touching the host network.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
FUNCTIONS = re.search(r"# --- UI address \(#507\) begin ---\n(.*?)# --- UI address \(#507\) end ---",
                      BOOTSTRAP, re.S).group(1)
ENV_VALUE = re.search(r"env_value\(\)\{\n.*?\n\}\n", BOOTSTRAP, re.S).group(0)
FINAL = BOOTSTRAP[BOOTSTRAP.index("mapfile -t ui_bases"):BOOTSTRAP.index("[[ -z $setup_token ]]")]

MULTI_NIC = """1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 172.31.104.66/20 brd 172.31.111.255 scope global dynamic eth0
3: eth1    inet 10.10.10.10/24 brd 10.10.10.255 scope global eth1
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0"""


def run(tmp_path, ip_output, *, env_file="", hostname_i="", fqdn="vertep-host", installed=True, extra=None):
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    (tools / "ip").write_text(f"#!/bin/sh\ncat <<'EOF'\n{ip_output}\nEOF\n", encoding="utf-8")
    (tools / "hostname").write_text(
        f"#!/bin/sh\ncase \"$1\" in -I) echo '{hostname_i}' ;; *) echo '{fqdn}' ;; esac\n", encoding="utf-8")
    (tools / "xdg-open").write_text(f"#!/bin/sh\necho \"$1\" > '{tmp_path}/opened'\n", encoding="utf-8")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    (tmp_path / "opened").unlink(missing_ok=True)
    root = tmp_path / "install"
    root.mkdir(exist_ok=True)
    (root / ".env").write_text(env_file, encoding="utf-8")
    script = (f"set -euo pipefail\nINSTALL_ROOT='{root}'\ninstallation_complete={'true' if installed else 'false'}\n"
              f"setup_token=abc123\n{FUNCTIONS}{ENV_VALUE}{FINAL}")
    environment = {"PATH": f"{tools}:/usr/bin:/bin", **(extra or {})}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0, result.stderr
    opened = tmp_path / "opened"
    return result.stdout, opened.read_text().strip() if opened.exists() else None


def test_multi_nic_lists_every_reachable_address_instead_of_the_first(tmp_path):
    out, opened = run(tmp_path, MULTI_NIC)
    assert "https://172.31.104.66:8443/" in out and "https://10.10.10.10:8443/" in out
    assert "172.17.0.1" not in out  # Docker bridge is not a UI address
    assert "several network addresses" in out and "PUBLIC_URL" in out
    assert opened is None  # nothing is promoted as the single canonical address


def test_configured_public_url_wins_for_update_and_first_run(tmp_path):
    out, opened = run(tmp_path, MULTI_NIC, env_file="PUBLIC_URL=https://10.10.10.10:8443\n")
    assert "Open https://10.10.10.10:8443/ in your browser." in out
    assert "172.31.104.66" not in out and opened == "https://10.10.10.10:8443/"
    out, opened = run(tmp_path, MULTI_NIC, env_file="PUBLIC_URL=https://vertep.lan:8443/\n", installed=False)
    assert "Open https://vertep.lan:8443/setup?token=abc123 in your browser to complete the First Run Wizard." in out
    assert opened == "https://vertep.lan:8443/setup?token=abc123"


def test_explicit_web_domain_is_used_but_the_default_host_name_is_not(tmp_path):
    out, _ = run(tmp_path, MULTI_NIC, env_file="WEB_DOMAIN=vertep.example\n")
    assert "Open https://vertep.example:8443/ in your browser." in out
    out, _ = run(tmp_path, MULTI_NIC, extra={"VERTEP_WEB_DOMAIN": "10.10.10.10"})
    assert "Open https://10.10.10.10:8443/ in your browser." in out
    out, _ = run(tmp_path, MULTI_NIC, env_file="WEB_DOMAIN=vertep-host\n")  # bootstrap's own default
    assert "several network addresses" in out


@pytest.mark.parametrize("installed, suffix", [(True, ""), (False, "setup?token=abc123")])
def test_single_nic_opens_its_only_address_in_both_scenarios(tmp_path, installed, suffix):
    out, opened = run(tmp_path, "2: eth0    inet 192.168.1.20/24 scope global eth0", installed=installed)
    assert f"https://192.168.1.20:8443/{suffix}" in out and opened == f"https://192.168.1.20:8443/{suffix}"


def test_ipv6_is_bracketed_and_missing_addresses_give_a_clear_message(tmp_path):
    out, opened = run(tmp_path, "2: eth0    inet6 2001:db8::10/64 scope global eth0")
    assert "https://[2001:db8::10]:8443/" in out and opened == "https://[2001:db8::10]:8443/"
    out, opened = run(tmp_path, "", hostname_i="")
    assert "Could not determine the server address" in out and opened is None
    out, _ = run(tmp_path, "", env_file="PUBLIC_URL=not a url\n", hostname_i="127.0.0.1 10.0.0.5")
    assert "Open https://10.0.0.5:8443/ in your browser." in out  # invalid PUBLIC_URL is ignored


def test_bootstrap_no_longer_takes_the_first_hostname_address():
    assert "hostname -I | awk '{print $1}'" not in BOOTSTRAP
