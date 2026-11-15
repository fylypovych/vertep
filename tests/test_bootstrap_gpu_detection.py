"""bootstrap.sh refuses a ComfyUI role on a host without a GPU, before starting anything.

Observed on a Hyper-V VM without GPU passthrough: the ``gpu`` role started the
CUDA ComfyUI image, which crash-looped with "Found no NVIDIA driver on your
system" until the 10-minute runtime health timeout. There is no automatic CPU
fallback (AGENTS.md §14); the precondition now fails at once with its cause.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")

GUARD = re.compile(r'if printf .%s\\n. "\$\{role_services\[@\]\}" \| grep -qx comfyui '
                   r'&& \[\[ \$gpu_vendor == none \]\]; then\n.*?\nfi', re.S)


def _guard() -> str:
    match = GUARD.search(BOOTSTRAP)
    assert match, "the GPU precondition for ComfyUI roles is missing"
    return match.group(0)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("vendor,services,role,extra,fails", [
    ("none", ["core", "comfyui"], "gpu", "", True),            # Hyper-V VM without passthrough
    ("none", ["core", "worker", "comfyui"], "core", "gpu", True),
    ("nvidia", ["core", "comfyui"], "gpu", "", False),
    ("amd", ["core", "comfyui"], "gpu", "", False),
    ("none", ["core", "worker"], "core", "", False),            # no ComfyUI: no GPU needed
])
def test_comfyui_role_needs_a_detected_gpu(vendor, services, role, extra, fails):
    script = (
        'fail(){ printf "VERTEP: %s\\n" "$*" >&2; exit 1; }\n'
        f"gpu_vendor={vendor}; NODE_ROLE={role}; ADDITIONAL_ROLES={extra}\n"
        f"role_services=({' '.join(services)})\n"
        + _guard() + "\necho passed\n")
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    if fails:
        assert result.returncode == 1
        assert "runs ComfyUI and needs an NVIDIA or AMD GPU, but none was detected" in result.stderr
        assert f"role {role}" in result.stderr
    else:
        assert result.returncode == 0 and result.stdout.strip() == "passed", result.stderr


def test_the_precondition_runs_before_the_runtime_is_written_or_started():
    position = BOOTSTRAP.index(_guard())
    assert position < BOOTSTRAP.index('env_tmp=$(mktemp "$INSTALL_ROOT/.env.XXXXXX")')
    assert position < BOOTSTRAP.index('progress "Starting Vertep $version"')
    # GPU detection itself is unchanged.
    assert ("if grep -qix '0x10de' /sys/bus/pci/devices/*/vendor 2>/dev/null "
            "|| lspci 2>/dev/null | grep -qi nvidia; then") in BOOTSTRAP
