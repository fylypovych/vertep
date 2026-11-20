"""Host scripts of the signed runtime bundle run without a source checkout.

vertep-deployment.service runs scripts/apply-deployment.py from /opt/vertep with
the host's python3; the bundle carries core/ and db/, not the repository. A
missing import (0.0.4.17: ``adapters``) failed every deployment request and
systemd disabled the unit (start-limit-hit). The real bundle is built here and
each host-side script is started with an isolated system interpreter.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_environment_readiness import load_script

ROOT = Path(__file__).resolve().parents[1]
# Executed on the host by systemd units or the `vertep` CLI (migrate.py runs in its container).
HOST_SCRIPTS = ("apply-deployment.py", "startup-recovery.py", "update-agent.py", "host-health.py",
                "remove-vertep.py", "status.py", "watchdog.py", "agent-slots.py")
SYSTEM_PYTHON = shutil.which("python3", path="/usr/bin:/bin")


@pytest.mark.skipif(SYSTEM_PYTHON is None, reason="needs the system python3 the host uses")
def test_every_host_script_of_the_bundle_starts_with_the_system_interpreter(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    builder = load_script("build-runtime-bundle.py")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "private.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    roles = json.loads((ROOT / "config/node_roles.json").read_text())
    services = {item for definition in roles.values() if isinstance(definition, dict)
                for item in definition.get("services", [])}
    lock = tmp_path / "images.json"
    lock.write_text(json.dumps({name: {"reference": f"ghcr.io/fylypovych/vertep/{name}",
                                       "digest": "sha256:" + "a" * 64, "platforms": ["linux/amd64"]}
                                for name in services}))
    bundle = builder.build(ROOT, tmp_path / "opt-vertep", (ROOT / "VERSION").read_text().strip(), 1000,
                           lock, private, "a" * 40)
    for script in HOST_SCRIPTS:
        # -I: no PYTHONPATH, user site or repository on sys.path, as on the host.
        result = subprocess.run([SYSTEM_PYTHON, "-I", str(bundle / "scripts" / script), "--help"],
                                cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"{script}: {result.stderr[-1500:]}"
