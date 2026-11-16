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


# ── Owner decision 16.11.2026: an update goes through; the additional gpu role is switched off ──

DISABLE = re.compile(r'gpu_role_disabled=false\n.*?\nfi\n', re.S)
PLAN_FIX = re.compile(r'if \[\[ \$gpu_role_disabled == true \]\]; then\n.*?\nPY\nfi\n', re.S)


def _disable(vendor, role, extra):
    match = DISABLE.search(BOOTSTRAP)
    assert match, "switching the gpu role off is missing"
    script = (f"gpu_vendor={vendor}; NODE_ROLE={role}; ADDITIONAL_ROLES='{extra}'\n" + match.group(0)
              + 'printf "%s|%s" "$ADDITIONAL_ROLES" "$gpu_role_disabled"\n')
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("vendor,extra,expected,disabled", [
    ("none", "gpu,text,voice,publisher,backup,monitoring", "text,voice,publisher,backup,monitoring", "true"),
    ("none", "gpu", "", "true"),
    ("none", "text, gpu", "text", "true"),
    ("nvidia", "gpu,text", "gpu,text", "false"),
    ("none", "text,voice", "text,voice", "false"),
])
def test_the_additional_gpu_role_is_switched_off_without_a_gpu(vendor, extra, expected, disabled):
    out, err = _disable(vendor, "core", extra)
    assert out == f"{expected}|{disabled}"
    assert ("gpu (ComfyUI) is switched off" in err) == (disabled == "true")


def test_switching_off_happens_before_services_and_the_env_are_derived():
    position = DISABLE.search(BOOTSTRAP).start()
    assert position < BOOTSTRAP.index("additional_services=()")
    assert position < BOOTSTRAP.index('env_tmp=$(mktemp "$INSTALL_ROOT/.env.XXXXXX")')
    assert position > BOOTSTRAP.index('gpu_vendor=none; gpu_name="GPU not found"')


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="bash and python3 are required")
def test_the_applied_plan_drops_the_gpu_role(tmp_path):
    import json
    import sys
    sys.path.insert(0, str(ROOT))
    from core.deployment_plan import create_plan, verify_plan
    catalog = json.loads((ROOT / "config/node_roles.json").read_text(encoding="utf-8"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/node_roles.json").write_text(json.dumps(catalog), encoding="utf-8")
    roles = ["gpu", "text", "voice", "publisher", "backup", "monitoring"]
    plan = create_plan(catalog, "core", "0.0.3.79", roles)
    (tmp_path / "config/deployment-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    script = f"INSTALL_ROOT={tmp_path}; gpu_role_disabled=true\n" + PLAN_FIX.search(BOOTSTRAP).group(0)
    subprocess.run([shutil.which("bash"), "-c", script], check=True, timeout=30)
    fixed = json.loads((tmp_path / "config/deployment-plan.json").read_text(encoding="utf-8"))
    assert fixed == create_plan(catalog, "core", "0.0.3.79", roles[1:])
    assert verify_plan(fixed) and "comfyui" not in fixed["services"] and "worker" in fixed["services"]
