"""bootstrap.sh runs with umask 077, but apt verifies repository signatures as the
unprivileged _apt user: every keyring it writes must be made world-readable, or
apt rejects the repository as "not signed" (NVIDIA Container Toolkit, ROCm)."""

import re
from pathlib import Path


def test_every_dearmored_apt_keyring_is_readable_by_apt():
    text = (Path(__file__).parents[1] / "bootstrap.sh").read_text(encoding="utf-8")
    assert "umask 077" in text
    keyrings = re.findall(r"gpg [^\n]*--dearmor -o (\S+)", text)
    assert keyrings, "bootstrap installs signed third-party repositories"
    for keyring in keyrings:
        assert re.search(r"chmod 0644 [^\n]*" + re.escape(keyring), text), keyring
        # A rerun must replace the key non-interactively instead of failing on "File exists".
        assert re.search(r"gpg --batch --yes --dearmor -o " + re.escape(keyring), text), keyring
