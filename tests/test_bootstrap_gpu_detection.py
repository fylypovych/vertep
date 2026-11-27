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
    # PCI detection of NVIDIA is kept; Hyper-V GPU-P is detected in addition.
    assert ("if grep -qix '0x10de' /sys/bus/pci/devices/*/vendor 2>/dev/null "
            "|| lspci 2>/dev/null | grep -qi nvidia \\\n") in BOOTSTRAP


# ── Hyper-V GPU-P: Microsoft PCI device 0x1414, /dev/dxg, driver from the host in /usr/lib/wsl/lib ──

def _detect(tmp_path, *, dxg, smi_output):
    """Run the detection block with /dev/dxg, sysfs and /usr/lib/wsl/lib redirected to tmp_path."""
    start = BOOTSTRAP.index('gpu_vendor=none; gpu_name="GPU not found"')
    block = BOOTSTRAP[start:BOOTSTRAP.index('progress "Installing container runtime"')]
    pci = tmp_path / "pci" / "0000:00:00.0"
    pci.mkdir(parents=True)
    (pci / "vendor").write_text("0x1414\n")
    wsl = tmp_path / "wsl"
    wsl.mkdir()
    if smi_output is not None:
        smi = wsl / "nvidia-smi"
        smi.write_text("#!/bin/sh\n" + smi_output)
        smi.chmod(0o755)
    block = (block.replace("/dev/dxg", "/dev/null" if dxg else str(tmp_path / "no-dxg"))
             .replace("/sys/bus/pci/devices", str(tmp_path / "pci"))
             .replace("/usr/lib/wsl/lib", str(wsl)))
    script = ("PATH=/usr/bin:/bin\nlspci(){ echo '00:00.0 3D controller: Microsoft Corporation Basic Render Driver'; }\n"
              + block + 'printf "%s|%s|%s|%s|%s" "$gpu_vendor" "$gpu_name" "$gpu_vram_mb" "$driver" "$cuda"\n')
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


SMI = ('case "$1" in -L) echo "GPU 0: NVIDIA GeForce RTX 3060 Laptop GPU (UUID: GPU-1)";;\n'
       '--query-gpu=*) echo "NVIDIA GeForce RTX 3060 Laptop GPU, 6144, 621.14";;\n'
       '*) echo "| NVIDIA-SMI 620.70.03   KMD Version: 621.14   CUDA UMD Version: 13.5 |";; esac\n')


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_gpu_p_nvidia_is_detected(tmp_path):
    """The VERTEP-TEST-2 stand: RTX 3060 shared from the Hyper-V host."""
    assert _detect(tmp_path, dxg=True, smi_output=SMI) == \
        "nvidia|NVIDIA GeForce RTX 3060 Laptop GPU| 6144| 621.14|13.5"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("dxg,smi_output", [
    (False, SMI),    # driver files without the dxgkrnl device
    (True, None),    # dxgkrnl without the host driver files
    (True, "exit 9\n"),  # nvidia-smi present but not working
])
def test_a_plain_hyper_v_vm_stays_without_gpu(tmp_path, dxg, smi_output):
    assert _detect(tmp_path, dxg=dxg, smi_output=smi_output).startswith("none|GPU not found|0|")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("lspci,vendor", [
    ("00:00.0 3D controller: Microsoft Corporation Basic Render Driver", "none"),  # "ATI" in "Corporation"
    ("00:02.0 VGA compatible controller: Intel Corporation UHD Graphics", "none"),
    ("00:00.0 Host bridge: Advanced Micro Devices, Inc. [AMD] Root Complex", "none"),  # AMD CPU, not a GPU
    ("03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 31", "amd"),
])
def test_amd_is_detected_only_from_a_display_device(lspci, vendor):
    start = BOOTSTRAP.index('gpu_vendor=none; gpu_name="GPU not found"')
    block = BOOTSTRAP[start:BOOTSTRAP.index('progress "Installing container runtime"')]
    block = block.replace("/dev/dxg", "/nonexistent/dxg").replace("/sys/bus/pci/devices", "/nonexistent")
    script = f"PATH=/usr/bin:/bin\nlspci(){{ echo '{lspci}'; }}\n" + block + 'printf "%s" "$gpu_vendor"\n'
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert result.stdout == vendor, result.stderr


def test_gpu_p_never_installs_a_native_driver():
    block = BOOTSTRAP[BOOTSTRAP.index("if [[ $gpu_vendor == nvidia && $driver == unavailable ]]; then"):]
    assert block.index("[[ ! -c /dev/dxg ]] || fail") < block.index("ubuntu-drivers install")


# ── Owner decision 16.11.2026: an update goes through; the additional gpu role is switched off ──

DISABLE = re.compile(r'gpu_role_disabled=false\n.*?\nfi\n', re.S)
PLAN_SYNC = re.compile(r'if \[\[ \$NODE_ROLE != unassigned \]\]; then\npython3 - "\$applied_plan".*?\nPY\nfi\n', re.S)


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


def _plan_sync(tmp_path, plan, role, additional):
    import json
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    (tmp_path / "config/node_roles.json").write_text(
        (ROOT / "config/node_roles.json").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "runtime/deployment-plan.py").write_text(
        (ROOT / "core/deployment_plan.py").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config/deployment-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    script = (f"INSTALL_ROOT={tmp_path}; applied_plan=$INSTALL_ROOT/config/deployment-plan.json\n"
              f"NODE_ROLE={role}; ADDITIONAL_ROLES='{additional}'\n"
              'fail(){ echo "FAIL $*" >&2; exit 1; }\n' + PLAN_SYNC.search(BOOTSTRAP).group(0))
    subprocess.run([shutil.which("bash"), "-c", script], check=True, timeout=30)
    return json.loads((tmp_path / "config/deployment-plan.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="bash and python3 are required")
def test_the_applied_plan_drops_the_gpu_role(tmp_path):
    import json
    import sys
    sys.path.insert(0, str(ROOT))
    from core.deployment_plan import create_plan, verify_plan
    catalog = json.loads((ROOT / "config/node_roles.json").read_text(encoding="utf-8"))
    roles = ["gpu", "text", "voice", "publisher", "backup", "monitoring"]
    fixed = _plan_sync(tmp_path, create_plan(catalog, "core", "0.0.3.79", roles), "core",
                       ",".join(roles[1:]))
    assert fixed == create_plan(catalog, "core", "0.0.3.79", roles[1:])
    assert verify_plan(fixed) and "comfyui" not in fixed["services"] and "worker" in fixed["services"]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="bash and python3 are required")
def test_a_plan_that_already_matches_is_left_untouched(tmp_path):
    import json
    import sys
    sys.path.insert(0, str(ROOT))
    from core.deployment_plan import create_plan
    catalog = json.loads((ROOT / "config/node_roles.json").read_text(encoding="utf-8"))
    plan = create_plan(catalog, "core", "0.0.3.70", ["text", "backup"], ["moneyprinter"])
    assert _plan_sync(tmp_path, plan, "core", "backup,text") == plan  # engine runtimes kept


# ── Owner decision 16.11.2026: the applied plan, not .env, is the source of the roles ──

ROLE_SOURCE = re.compile(r'applied_plan="\$INSTALL_ROOT/config/deployment-plan.json"\n.*?\nfi\nif \[\[ -n \$plan_role.*?\nfi\n', re.S)


def _roles(tmp_path, env_roles, plan=None, override_role="", override_extra=""):
    import json
    (tmp_path / "config").mkdir(exist_ok=True)
    if plan is not None:
        (tmp_path / "config/deployment-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    script = (f"INSTALL_ROOT={tmp_path}\nexisting_node_role=core; existing_additional_roles='{env_roles}'\n"
              f"VERTEP_ROLE='{override_role}'; VERTEP_ADDITIONAL_ROLES='{override_extra}'\n"
              + ROLE_SOURCE.search(BOOTSTRAP).group(0) + 'printf "%s|%s" "$NODE_ROLE" "$ADDITIONAL_ROLES"\n')
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required")
def test_the_applied_plan_wins_over_a_stale_env(tmp_path):
    """The test stand: .env kept six roles, the plan applied from the Web held three."""
    out, err = _roles(tmp_path, "gpu,text,voice,publisher,backup,monitoring",
                      {"role": "core", "additional_roles": ["backup", "monitoring", "text"]})
    assert out == "core|backup,monitoring,text"
    assert "roles follow the applied deployment plan" in err


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required")
@pytest.mark.parametrize("plan,override_extra,expected", [
    (None, "", "core|text,voice"),                                           # no plan yet: .env
    ({"role": "unassigned", "services": []}, "", "core|text,voice"),         # bootstrap placeholder plan
    ({"role": "core", "additional_roles": ["text"]}, "backup", "core|backup"),  # automation override
    ({"role": "core"}, "", "core|"),                                          # plan without extras
])
def test_role_source_order(tmp_path, plan, override_extra, expected):
    out, _ = _roles(tmp_path, "text,voice", plan, override_extra=override_extra)
    assert out == expected


REMOVE_DROPPED = re.compile(r'mapfile -t kept_services < <\(.*?\ndone < <\("\$\{compose\[@\]\}" ps -a --services 2>/dev/null \|\| true\)\n', re.S)


def _remove_dropped(tmp_path, existing, role_services, plan_services=None):
    import json
    plan = tmp_path / "deployment-plan.json"
    if plan_services is not None:
        plan.write_text(json.dumps({"services": plan_services}), encoding="utf-8")
    match = REMOVE_DROPPED.search(BOOTSTRAP)
    assert match, "removing the services of dropped roles is missing"
    script = (
        "removed=()\n"
        f'fake(){{ case "$*" in "ps -a --services") printf "%s\\n" {" ".join(existing)};; '
        'rm\\ -sf\\ *) removed+=("${@: -1}");; esac; }\n'
        f"compose=(fake); applied_plan={plan}; role_services=({' '.join(role_services)})\n"
        + match.group(0) + 'printf "%s " "${removed[@]}"\n')
    result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split()), result.stderr


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required")
def test_the_containers_of_dropped_roles_are_removed(tmp_path):
    """The test stand after 0.0.3.83: comfyui kept restarting; tts/publisher-worker left the plan."""
    removed, err = _remove_dropped(
        tmp_path, ["core", "worker", "ollama", "comfyui", "tts", "publisher-worker"],
        ["core", "worker", "ollama"], ["core", "worker", "ollama"])
    assert removed == {"comfyui", "tts", "publisher-worker"}
    assert "removing service comfyui" in err


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required")
def test_engine_runtime_services_of_the_plan_are_kept(tmp_path):
    removed, _ = _remove_dropped(tmp_path, ["core", "worker", "moneyprinter"], ["core", "worker"],
                                 ["core", "worker", "moneyprinter"])
    assert removed == set()


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required")
def test_without_a_plan_only_the_role_services_count(tmp_path):
    removed, _ = _remove_dropped(tmp_path, ["proxy", "core", "comfyui"], ["proxy", "core"])
    assert removed == {"comfyui"}


def test_dropped_services_are_removed_after_the_selected_services_start():
    position = REMOVE_DROPPED.search(BOOTSTRAP).start()
    assert BOOTSTRAP.index('"${compose[@]}" up -d --remove-orphans "${role_services[@]}"') < position
    assert position < BOOTSTRAP.index("deadline=$((SECONDS+600))")
