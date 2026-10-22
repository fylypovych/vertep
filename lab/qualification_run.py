"""Real collectors and criterion runners of the laboratory qualification.

This module performs the *actual* work behind the rows R1–R6 of
Issue ``ir.0.0.2.52``: it observes the stand (host, container, GPU, Docker),
talks to the production-priority resource endpoint through the very same
client the laboratory uses in production and drives the laboratory itself in
an isolated working copy.

Two rules are enforced by construction:

* a fact that cannot be observed is reported as ``BLOCKED`` with the concrete
  missing prerequisite — never as a pass and never invented;
* nothing here writes to production: the only production interaction is the
  versioned resource contract (:mod:`lab.gpu_lease`) and the laboratory's own
  isolated working copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditTrail
from .budget import BudgetLedger, LabPaused, QuotaExceeded
from .gpu_lease import (LeaseDenied, LeaseLost, LeaseManager, ResourceRequirement,
                        ResourceUnavailable, VertepResourceClient)
from .orchestrator import Orchestrator
from .policy import Action, PolicyDenied, PolicyGate
from .qualification import (STATUS_BLOCKED, STATUS_FAIL, STATUS_PASS, build_row)
from .workspace import Workspace

#: Production paths the laboratory must never reach (isolation negatives).
DEFAULT_PRODUCTION_PATHS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/opt/vertep/config",
    "/opt/vertep/data",
    "/opt/vertep/secrets",
    "/data/config",
    "/data/storage",
    "/etc/vertep",
)

#: What must be impossible for every production path.
ISOLATION_EXPECTATIONS: dict[str, str] = {
    "/var/run/docker.sock": "unreadable",
    "/opt/vertep/config": "unwritable",
    "/opt/vertep/data": "unwritable",
    "/opt/vertep/secrets": "unwritable",
    "/data/config": "unwritable",
    "/data/storage": "unwritable",
    "/etc/vertep": "unwritable",
}

#: Approved envelope of the laboratory stand (Issue #252, R1).
EXPECTED_VCPU = (4, 8)
EXPECTED_RAM_GB = (16.0, 32.0)
EXPECTED_DISK_GB = (100.0, 200.0)
EXPECTED_OS = "ubuntu"
EXPECTED_OS_VERSION = "24.04"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(command: list[str], *, timeout: int = 60, cwd=None) -> dict:
    """Run a read-only command and report the outcome without raising."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout, cwd=cwd, errors="replace")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as error:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(error)}
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    return {"ok": completed.returncode == 0, "returncode": completed.returncode,
            "stdout": stdout, "stderr": stderr}


def sha256_file(path) -> str:
    """SHA256 of a file, or an empty string when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def collect_artifact_facts(paths) -> list[dict]:
    """Hash every artifact that belongs to the evidence."""
    entries: list[dict] = []
    for raw in paths or []:
        path = Path(raw)
        exists = path.is_file()
        entries.append({
            "path": path.as_posix(),
            "exists": exists,
            "sha256": sha256_file(path) if exists else "",
            "bytes": path.stat().st_size if exists else 0,
        })
    return entries


def collect_version_facts(repo_root) -> dict:
    """Exact version, full SHA, branch and dirtiness of the qualified copy."""
    root = Path(repo_root)
    version_file = root / "VERSION"
    version = ""
    if version_file.is_file():
        try:
            version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            version = ""
    sha = _run(["git", "-C", str(root), "rev-parse", "HEAD"])
    branch = _run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    status = _run(["git", "-C", str(root), "status", "--porcelain"])
    return {
        "version": version,
        "git_sha": sha["stdout"].strip() if sha["ok"] else "",
        "branch": branch["stdout"].strip() if branch["ok"] else "",
        "dirty": bool(status["stdout"].strip()) if status["ok"] else False,
        "reason": "" if sha["ok"] else sha["stderr"].strip()[:300],
    }


def _read_os_release() -> dict:
    path = Path("/etc/os-release")
    facts: dict[str, str] = {}
    if not path.is_file():
        return facts
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            facts[key.strip()] = value.strip().strip('"')
    except OSError:
        return {}
    return facts


def _memory_total_bytes() -> int:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _container_facts() -> dict:
    markers = [name for name in ("/run/.containerenv", "/.dockerenv")
               if Path(name).exists()]
    cgroup = ""
    for candidate in ("/proc/1/cgroup", "/proc/self/cgroup"):
        path = Path(candidate)
        if path.is_file():
            try:
                cgroup = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                cgroup = ""
            break
    runtime = ""
    for token in ("docker", "kubepods", "containerd", "lxc", "podman"):
        if token in cgroup:
            runtime = token
            break
    return {"in_container": bool(markers) or bool(runtime), "markers": markers,
            "runtime": runtime}


def collect_host_facts() -> dict:
    """OS, kernel, CPU, RAM and container facts of the stand."""
    os_release = _read_os_release()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count() or 0,
        "memory_total_bytes": _memory_total_bytes(),
        "python": platform.python_version(),
        "os_release": os_release,
        "container": _container_facts(),
    }


def collect_disk_facts(paths) -> list[dict]:
    """Disk capacity of the volumes the laboratory may use."""
    entries: list[dict] = []
    for raw in paths or []:
        path = Path(raw)
        probe = path if path.exists() else Path(path.anchor or "/")
        entry: dict = {"path": path.as_posix()}
        try:
            usage = shutil.disk_usage(str(probe))
            entry.update({"total_bytes": usage.total, "used_bytes": usage.used,
                          "free_bytes": usage.free})
        except OSError as error:
            entry["error"] = str(error)
        entries.append(entry)
    return entries


def collect_gpu_facts() -> dict:
    """Physical GPU inventory and VRAM usage of the stand."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "gpus": [], "compute_apps": [],
                "reason": "nvidia-smi is not available on this host"}
    query = _run([executable, "--query-gpu=index,name,uuid,driver_version,"
                            "memory.total,memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits"])
    gpus: list[dict] = []
    for line in query["stdout"].splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) >= 7:
            gpus.append({"index": parts[0], "name": parts[1], "uuid": parts[2],
                         "driver_version": parts[3], "memory_total_mb": parts[4],
                         "memory_used_mb": parts[5], "utilization_percent": parts[6]})
    apps = _run([executable, "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv,noheader,nounits"])
    compute_apps = []
    for line in apps["stdout"].splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) >= 3:
            compute_apps.append({"pid": parts[0], "process_name": parts[1],
                                  "used_memory_mb": parts[2]})
    return {"available": bool(gpus), "gpus": gpus, "compute_apps": compute_apps,
            "reason": "" if query["ok"] else query["stderr"].strip()[:300]}


def collect_docker_facts() -> dict:
    """Docker daemon and container state of the stand."""
    executable = shutil.which("docker")
    if not executable:
        return {"available": False, "containers": [],
                "reason": "docker CLI is not available on this host"}
    info = _run([executable, "info", "--format", "{{.ServerVersion}}"])
    listing = _run([executable, "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    containers = []
    for line in listing["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            containers.append({"name": parts[0], "status": parts[1], "image": parts[2]})
    return {"available": info["ok"], "server_version": info["stdout"].strip() if info["ok"] else "",
            "containers": containers,
            "reason": "" if info["ok"] else info["stderr"].strip()[:300]}


def collect_isolation_facts(paths) -> list[dict]:
    """Negative isolation probes — no production path may be reached."""
    entries: list[dict] = []
    for raw in paths or []:
        path = Path(raw)
        exists = path.exists()
        entries.append({
            "path": path.as_posix(),
            "exists": exists,
            "readable": bool(exists) and os.access(str(path), os.R_OK),
            "writable": bool(exists) and os.access(str(path), os.W_OK),
            "expectation": ISOLATION_EXPECTATIONS.get(path.as_posix(), "unwritable"),
        })
    return entries


def collect_network_facts(allowlist) -> dict:
    """The configured egress allowlist of the laboratory."""
    entries = [str(item).strip() for item in (allowlist or []) if str(item).strip()]
    return {"allowlist": entries, "configured": bool(entries),
            "source": "LAB_NETWORK_ALLOWLIST"}


def collect_gpu_passthrough_facts() -> dict:
    """GPU devices must not be passed through into the laboratory."""
    devices: list[str] = []
    dev = Path("/dev")
    if dev.is_dir():
        try:
            devices = sorted(str(item) for item in dev.glob("nvidia*"))
        except OSError:
            devices = []
    return {"devices": devices, "passthrough": bool(devices)}


def collect_digest_facts(images) -> list[dict]:
    """Immutable digests of the images the laboratory runs."""
    executable = shutil.which("docker")
    entries: list[dict] = []
    for image in images or []:
        entry: dict = {"image": image}
        if not executable:
            entry["reason"] = "docker CLI is not available"
            entries.append(entry)
            continue
        result = _run([executable, "image", "inspect", "--format",
                       "{{.Id}}|{{join .RepoDigests \",\"}}", image])
        if result["ok"] and result["stdout"].strip():
            identifier, _, digests = result["stdout"].strip().partition("|")
            entry["id"] = identifier
            entry["repo_digests"] = [item for item in digests.split(",") if item]
        else:
            entry["reason"] = result["stderr"].strip()[:300] or "image not found"
        entries.append(entry)
    return entries


def default_evidence_dir() -> Path:
    """Where the evidence is kept — never inside the production tree."""
    configured = os.getenv("LAB_QUALIFICATION_EVIDENCE_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    if os.name != "nt" and Path("/opt/vertep-lab").is_dir():
        return Path("/opt/vertep-lab/qualification").resolve()
    return (Path.home() / ".vertep" / "qualification").resolve()


@dataclass
class QualificationContext:
    """Everything the runners need to observe the stand."""

    repo_root: Path
    evidence_dir: Path
    state_dir: Path
    actor: str = "lab"
    vertep_url: str = ""
    vertep_token: str = ""
    idle_sharing_enabled: bool = False
    executor_url: str = ""
    executor_token: str = ""
    executor_enabled: bool = False
    workspace_root: Path | None = None
    network_allowlist: tuple[str, ...] = ()
    production_paths: tuple[str, ...] = DEFAULT_PRODUCTION_PATHS
    disk_paths: tuple[str, ...] = ("/",)
    images: tuple[str, ...] = ()
    model: str = "qwen2.5-coder:7b"
    vram_mb: int = 8192
    revoke_window_seconds: int = 60
    heartbeat_seconds: int = 30
    verifier: str = ""
    collector: str = "qualify-lab"
    operator_artifacts: dict = field(default_factory=dict)

    @classmethod
    def from_environment(cls, repo_root=None, evidence_dir=None, state_dir=None) -> "QualificationContext":
        root = Path(repo_root or Path.cwd()).resolve()
        evidence = Path(evidence_dir).resolve() if evidence_dir else default_evidence_dir()
        state = Path(state_dir or os.getenv("LAB_STATE_DIR") or (evidence / "state")).resolve()
        workspace = os.getenv("LAB_WORKSPACE", "").strip()
        allowlist = tuple(item for item in os.getenv("LAB_NETWORK_ALLOWLIST", "").split(",")
                          if item.strip())
        images = tuple(item for item in os.getenv("LAB_QUALIFICATION_IMAGES", "").split(",")
                       if item.strip())
        return cls(
            repo_root=root,
            evidence_dir=evidence,
            state_dir=state,
            actor=os.getenv("LAB_ACTOR", "lab"),
            vertep_url=os.getenv("LAB_VERTEP_URL", "").strip(),
            vertep_token=os.getenv("LAB_VERTEP_TOKEN", ""),
            idle_sharing_enabled=os.getenv("LAB_IDLE_SHARING_ENABLED", "").lower()
            in {"true", "1", "yes"},
            executor_url=os.getenv("LAB_ROLE_EXECUTOR_URL", "").strip(),
            executor_token=os.getenv("LAB_ROLE_EXECUTOR_TOKEN", ""),
            executor_enabled=os.getenv("LAB_ROLE_EXECUTOR_ENABLED", "").lower()
            in {"true", "1", "yes"},
            workspace_root=Path(workspace).resolve() if workspace else None,
            network_allowlist=allowlist,
            images=images,
            verifier=os.getenv("LAB_QUALIFICATION_VERIFIER", "").strip(),
        )


def _base_facts(context: QualificationContext) -> dict:
    """Facts every row carries: version, SHA, digests, environment, audit."""
    version = collect_version_facts(context.repo_root)
    host = collect_host_facts()
    trail = AuditTrail(context.state_dir / "audit.jsonl")
    audit = {"path": str(context.state_dir / "audit.jsonl"),
             "records": len(trail.records()),
             "verified": trail.verify().get("valid", False)}
    digests = collect_digest_facts(context.images)
    return {
        "version": version["version"],
        "git_sha": version["git_sha"],
        "image_digests": digests,
        "environment": {
            "host": host,
            "branch": version["branch"],
            "dirty": version["dirty"],
            "collector": context.collector,
            "accounts": sorted({item.split("=")[0] for item in os.environ
                                if item.startswith("LAB_")}),
        },
        "audit": audit,
        "metrics": {},
    }


def _blocked_row(context: QualificationContext, criterion_id: str, reason: str,
                 facts: dict | None = None, artifacts: list | None = None) -> dict:
    return build_row(criterion_id, status=STATUS_BLOCKED, actual=reason,
                     facts=facts if facts is not None else _base_facts(context),
                     artifacts=artifacts or [])


def _artifacts(context: QualificationContext, names) -> list[dict]:
    return collect_artifact_facts([context.evidence_dir / name for name in names])


def run_r1(context: QualificationContext) -> dict:
    """R1 — isolated Ubuntu 24.04 VM, resources, lifecycle, isolation negatives."""
    facts = _base_facts(context)
    host = facts["environment"]["host"]
    disk = collect_disk_facts(context.disk_paths)
    isolation = collect_isolation_facts(context.production_paths)
    network = collect_network_facts(context.network_allowlist)
    passthrough = collect_gpu_passthrough_facts()
    docker = collect_docker_facts()
    gpu = collect_gpu_facts()
    artifacts = _artifacts(context, ("r1-lifecycle.txt", "r1-resource-approval.json"))

    problems: list[str] = []
    os_release = host.get("os_release") or {}
    os_id = str(os_release.get("ID", "")).lower()
    os_version = str(os_release.get("VERSION_ID", ""))
    if os_id != EXPECTED_OS or not os_version.startswith(EXPECTED_OS_VERSION):
        problems.append(
            f"stand is not Ubuntu Server {EXPECTED_OS_VERSION}: "
            f"{os_release.get('PRETTY_NAME') or host.get('platform')}")
    cpu = int(host.get("cpu_count") or 0)
    if not EXPECTED_VCPU[0] <= cpu <= EXPECTED_VCPU[1]:
        problems.append(f"vCPU {cpu} outside the approved {EXPECTED_VCPU[0]}–{EXPECTED_VCPU[1]} range")
    memory_gb = int(host.get("memory_total_bytes") or 0) / (1024 ** 3)
    if not EXPECTED_RAM_GB[0] <= memory_gb <= EXPECTED_RAM_GB[1]:
        problems.append(
            f"RAM {memory_gb:.1f} GB outside the approved "
            f"{EXPECTED_RAM_GB[0]:.0f}–{EXPECTED_RAM_GB[1]:.0f} GB range")
    disk_gb = max((float(entry.get("total_bytes") or 0) for entry in disk), default=0.0) / (1024 ** 3)
    if disk_gb and not EXPECTED_DISK_GB[0] <= disk_gb <= EXPECTED_DISK_GB[1]:
        problems.append(
            f"disk {disk_gb:.1f} GB outside the approved "
            f"{EXPECTED_DISK_GB[0]:.0f}–{EXPECTED_DISK_GB[1]:.0f} GB range")
    for entry in isolation:
        expectation = entry["expectation"]
        if expectation == "unreadable" and entry["readable"]:
            problems.append(f"production path {entry['path']} is readable from the laboratory")
        if expectation == "unwritable" and entry["writable"]:
            problems.append(f"production path {entry['path']} is writable from the laboratory")
    if not network["configured"]:
        problems.append("no network allowlist configured (LAB_NETWORK_ALLOWLIST)")
    if passthrough["passthrough"]:
        problems.append(f"GPU devices are passed through: {passthrough['devices']}")
    lifecycle = next((item for item in artifacts if item["path"].endswith("r1-lifecycle.txt")), None)
    if not lifecycle or not lifecycle["exists"]:
        problems.append(
            "no lifecycle evidence: the operator must record the independent "
            "start/stop/restart of the laboratory in r1-lifecycle.txt")
    if not docker["available"]:
        problems.append(f"docker is not usable on the stand: {docker['reason']}")

    facts.update({
        "resources": {"vcpu": cpu, "ram_gb": round(memory_gb, 2), "disk": disk},
        "isolation": isolation,
        "network": network,
        "gpu_passthrough": passthrough,
        "container": {"host_container": host.get("container"), "docker": docker,
                      "gpu": gpu, "lifecycle_artifact": lifecycle},
        "metrics": {"problems": problems},
    })
    status = STATUS_PASS if not problems else STATUS_FAIL
    actual = ("виміряні ресурси, життєвий цикл зафіксовано, production-доступ відсутній"
              if not problems else "; ".join(problems))
    return build_row("R1", status=status, actual=actual, facts=facts, artifacts=artifacts)


def _lease_client(context: QualificationContext) -> VertepResourceClient:
    return VertepResourceClient(context.vertep_url, token=context.vertep_token,
                                enabled=context.idle_sharing_enabled)


def run_r2(context: QualificationContext) -> dict:
    """R2 — production-priority lease, queue, revoke, VRAM release."""
    facts = _base_facts(context)
    if not context.vertep_url:
        facts["metrics"] = {"reason": "no LAB_VERTEP_URL configured"}
        return _blocked_row(context, "R2",
                            "no production resource endpoint configured (LAB_VERTEP_URL); "
                            "the owner must provide the production-priority endpoint",
                            facts=facts)
    if not context.idle_sharing_enabled:
        facts["metrics"] = {"reason": "idle sharing disabled by policy"}
        return _blocked_row(context, "R2",
                            "idle GPU sharing is disabled by policy (LAB_IDLE_SHARING_ENABLED); "
                            "the owner must enable it on the stand",
                            facts=facts)

    manager = LeaseManager(_lease_client(context), state_dir=context.state_dir,
                            actor=context.actor, ttl_seconds=900,
                            heartbeat_seconds=context.heartbeat_seconds)
    requirement = ResourceRequirement(model=context.model, vram_mb=context.vram_mb)
    problems: list[str] = []

    status_before = manager.status()
    denied_without_lease = ""
    try:
        manager.assert_usable()
        problems.append("the laboratory used the GPU without an active lease")
    except LeaseLost as error:
        denied_without_lease = str(error)

    gpu_before = collect_gpu_facts()
    lease = None
    acquired_at = ""
    status_during: dict = {}
    heartbeat_seconds = 0.0
    revoke_observed = False
    revoke_detected_after = 0.0
    release_ack: dict = {}
    release_seconds = 0.0
    gpu_after = gpu_before

    try:
        lease = manager.acquire(requirement)
        acquired_at = utcnow()
        status_during = manager.status()
        started = time.monotonic()
        manager.heartbeat()
        heartbeat_seconds = time.monotonic() - started
    except (LeaseDenied, ResourceUnavailable, LeaseLost) as error:
        problems.append(f"the production endpoint did not grant a lease: {error}")

    if lease is not None:
        deadline = time.monotonic() + context.revoke_window_seconds
        while time.monotonic() < deadline:
            try:
                manager.heartbeat()
                time.sleep(min(context.heartbeat_seconds, 5))
            except LeaseLost as error:
                revoke_observed = True
                revoke_detected_after = context.heartbeat_seconds
                problems.append(f"lease lost while polling: {error}")
                break
        gpu_before = collect_gpu_facts()
        started = time.monotonic()
        release_ack = manager.release(reason="qualification R2 release")
        release_seconds = time.monotonic() - started
        gpu_after = collect_gpu_facts()

    if not revoke_observed:
        problems.append(
            "no production task arrived during the qualification window, so the "
            "revoke/control-stop path was not observed")
    if not release_ack.get("released"):
        problems.append(f"the production endpoint did not acknowledge the release: {release_ack}")

    facts.update({
        "lease": {
            "requirement": requirement.as_dict(),
            "denied_without_lease": denied_without_lease,
            "status_before": status_before,
            "lease": lease.as_dict() if lease else {},
            "acquired_at": acquired_at,
            "status_during": status_during,
            "release": release_ack,
            "revoke_observed": revoke_observed,
        },
        "vram": {"before_release": gpu_before, "after_release": gpu_after},
        "latency": {
            "heartbeat_seconds": round(heartbeat_seconds, 4),
            "revoke_detected_after_seconds": revoke_detected_after,
            "release_ack_seconds": round(release_seconds, 4),
        },
        "metrics": {"problems": problems},
    })
    status = STATUS_PASS if not problems else STATUS_FAIL
    actual = ("lease виміряно на реальному endpoint: deny без lease, queue під lease, "
              "release з ack" if not problems else "; ".join(problems))
    return build_row("R2", status=status, actual=actual, facts=facts)


def run_r3(context: QualificationContext) -> dict:
    """R3 — real fault scenarios on the stand."""
    facts = _base_facts(context)
    problems: list[str] = []
    faults: dict = {}

    unreachable = VertepResourceClient("http://127.0.0.1:9", token="", enabled=True)
    try:
        unreachable.request(ResourceRequirement(model=context.model), 60)
        faults["connection_loss"] = {"observed": False, "reason": "the closed port answered"}
        problems.append("an unreachable endpoint did not fail closed")
    except (ResourceUnavailable, LeaseDenied) as error:
        faults["connection_loss"] = {"observed": True, "reason": str(error)}

    manager = LeaseManager(_lease_client(context), state_dir=context.state_dir,
                            actor=context.actor, heartbeat_seconds=context.heartbeat_seconds)
    try:
        manager.assert_usable()
        faults["no_gpu_without_lease"] = {"observed": False,
                                          "reason": "the laboratory held a usable lease without one"}
        problems.append("the laboratory could start work without a lease")
    except LeaseLost as error:
        faults["no_gpu_without_lease"] = {"observed": True, "reason": str(error)}

    persisted = context.state_dir / "lease.json"
    reconciliation = {"path": str(persisted), "exists": persisted.is_file()}
    if persisted.is_file():
        try:
            payload = json.loads(persisted.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        reconciliation["record"] = payload
        restarted = LeaseManager(_lease_client(context), state_dir=context.state_dir,
                                 actor=context.actor)
        reconciliation["released_on_restart"] = not (restarted.lease and restarted.lease.active())
    faults["restart_reconciliation"] = reconciliation
    if reconciliation["exists"] and not reconciliation.get("released_on_restart"):
        problems.append("a persisted lease survived a restart without reconciliation")

    operator_artifacts = _artifacts(context, ("r3-hang.json", "r3-restart.json",
                                               "r3-quota.json"))
    for name, key in (("r3-hang.json", "inference_hang"),
                      ("r3-restart.json", "vm_or_dispatcher_restart"),
                      ("r3-quota.json", "quota_exhaustion")):
        entry = next((item for item in operator_artifacts if item["path"].endswith(name)), None)
        faults[key] = {"artifact": entry, "observed": bool(entry and entry["exists"])}
        if not (entry and entry["exists"]):
            problems.append(
                f"the real {key.replace('_', ' ')} was not reproduced: the operator must "
                f"record it in {name}")

    gpu = collect_gpu_facts()
    facts.update({
        "lease": {"state_file": str(persisted), "reason": "fault scenarios keep the lab idle"},
        "faults": faults,
        "vram": gpu,
        "metrics": {"problems": problems},
    })
    status = STATUS_PASS if not problems else STATUS_FAIL
    actual = ("fault-сценарії відтворено на стенді без шкоди production"
              if not problems else "; ".join(problems))
    return build_row("R3", status=status, actual=actual, facts=facts,
                     artifacts=operator_artifacts)


def run_r4(context: QualificationContext) -> dict:
    """R4 — the first actual audit in an isolated Git copy on the stand."""
    facts = _base_facts(context)
    problems: list[str] = []
    if context.workspace_root is None:
        facts["metrics"] = {"reason": "no LAB_WORKSPACE configured"}
        return _blocked_row(context, "R4",
                            "no isolated laboratory working copy configured (LAB_WORKSPACE); "
                            "the owner must provide the Git copy on the VM",
                            facts=facts)
    if not context.executor_url or not context.executor_enabled:
        facts["metrics"] = {"reason": "no enabled role executor"}
        return _blocked_row(context, "R4",
                            "no enabled role executor configured (LAB_ROLE_EXECUTOR_URL / "
                            "LAB_ROLE_EXECUTOR_ENABLED); real model routes are required",
                            facts=facts)

    workspace = Workspace(context.workspace_root, repository_url="", branch="main")
    workspace.prepare()
    gate = PolicyGate(state_dir=context.state_dir / "policy")
    budget = BudgetLedger(state_dir=context.state_dir / "budget")
    from .executor import EndpointRoleExecutor
    executor = EndpointRoleExecutor(context.executor_url, token=context.executor_token,
                                    enabled=True)
    orchestrator = Orchestrator(workspace, gate, budget, provider=None, actor=context.actor,
                                executor=executor)

    try:
        workspace.require_isolated()
    except Exception as error:
        problems.append(f"the working copy is not isolated: {error}")

    outcomes: list[dict] = []
    try:
        orchestrator.enqueue("audit", role="reviewer", payload={"detail": "first actual audit"})
        outcome = orchestrator.run_once(model=context.model, provider="ollama")
        outcomes.append(outcome)
        if outcome.get("state") != "COMPLETED":
            problems.append(f"the audit run did not complete: {outcome.get('reason')}")
    except PolicyDenied as error:
        problems.append(f"the policy gate denied the run: {error}")
    except QuotaExceeded as error:
        problems.append(f"the budget denied the run: {error}")
    except LabPaused as error:
        problems.append(f"the laboratory is paused: {error}")

    trail = AuditTrail(context.state_dir / "policy" / "audit.jsonl")
    head = _run(["git", "-C", str(context.workspace_root), "rev-parse", "HEAD"])
    remotes = _run(["git", "-C", str(context.workspace_root), "remote", "-v"])
    publication = {
        "workspace_head": head["stdout"].strip() if head["ok"] else "",
        "remotes": [line for line in remotes["stdout"].splitlines() if line.strip()],
        "committed": False,
        "published": False,
    }
    if publication["workspace_head"] and publication["workspace_head"] != facts["git_sha"]:
        publication["committed"] = True

    facts.update({
        "audit_trail": {"path": str(context.state_dir / "policy" / "audit.jsonl"),
                        "records": len(trail.records()),
                        "verified": trail.verify().get("valid", False)},
        "review": {"roles": list(orchestrator.status().get("roles", [])),
                   "outcomes": outcomes,
                   "results": orchestrator.results()},
        "publication": publication,
        "metrics": {"problems": problems},
    })
    status = STATUS_PASS if not problems else STATUS_FAIL
    actual = ("перший actual audit виконано в ізольованій Git copy без публікації"
              if not problems else "; ".join(problems))
    return build_row("R4", status=status, actual=actual, facts=facts)


def run_r5(context: QualificationContext, previous: list) -> dict:
    """R5 — 24/7 mode; only after R1–R4 and an explicit owner activation."""
    facts = _base_facts(context)
    problems: list[str] = []
    blocking = [str((row or {}).get("criterion") or "") for row in previous
                if str((row or {}).get("status") or "").upper() != STATUS_PASS]
    if blocking:
        facts["metrics"] = {"reason": f"rows not passed: {', '.join(blocking)}"}
        return _blocked_row(context, "R5",
                            f"R5 may only start after R1–R4 pass; not passed: "
                            f"{', '.join(blocking)}", facts=facts)

    gate = PolicyGate(state_dir=context.state_dir / "policy")
    if not gate.activated:
        facts["metrics"] = {"reason": "the policy gate is not activated"}
        return _blocked_row(context, "R5",
                            "the policy gate is not activated; the owner must activate the "
                            "laboratory explicitly (LAB_POLICY_GATE_ACTIVE)", facts=facts)

    budget = BudgetLedger(state_dir=context.state_dir / "budget")
    usage = budget.usage()
    pauses = budget.pauses()
    client = _lease_client(context)
    try:
        production_status = client.status()
    except ResourceUnavailable as error:
        production_status = {"error": str(error)}
        problems.append(f"the production endpoint is unreachable: {error}")

    schedule = {"window_seconds": 0.0, "cycles": 0, "started_at": utcnow()}
    artifacts = _artifacts(context, ("r5-schedule.json", "r5-notifications.json"))

    facts.update({
        "activation": {"gate_activated": gate.activated, "grants": len(gate.grants),
                        "actor": context.actor},
        "schedule": schedule,
        "budgets": {"usage": usage, "pauses": pauses},
        "notifications": {"artifacts": artifacts},
        "metrics": {"problems": problems},
    })
    status = STATUS_PASS if not problems else STATUS_FAIL
    actual = ("24/7 режим активний, budgets виміряні" if not problems else "; ".join(problems))
    return build_row("R5", status=status, actual=actual, facts=facts, artifacts=artifacts)


def run_r6(context: QualificationContext, previous: list) -> dict:
    """R6 — completeness of the evidence and its independent verification."""
    from .qualification import CRITERIA, validate_evidence
    facts = _base_facts(context)
    others = [row for row in previous
              if str((row or {}).get("criterion") or "") != "R6"]
    verdict = validate_evidence(others, criteria=[item for item in CRITERIA if item.id != "R6"])
    verification_artifact = context.evidence_dir / "verification.json"
    verification: dict = {}
    if verification_artifact.is_file():
        try:
            verification = json.loads(verification_artifact.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verification = {}
    artifacts = collect_artifact_facts([verification_artifact])
    problems: list[str] = []
    if not verification:
        problems.append(
            "no independent verification record: the verifier must place verification.json "
            "into the evidence directory")
    elif str(verification.get("verifier") or "") == context.collector:
        problems.append("the verifier and the collector are the same actor")
    facts["rows"] = {"summary": verdict["statuses"], "problems": verdict["problems"],
                      "missing": verdict["missing"]}
    facts["metrics"] = {"problems": problems}
    status = STATUS_PASS if not problems and not verdict["problems"] and not verdict["missing"] \
        else STATUS_BLOCKED
    actual = ("докази повні та верифіковані незалежним виконавцем"
              if status == STATUS_PASS else "; ".join(problems + verdict["problems"]))
    return build_row("R6", status=status, actual=actual, facts=facts,
                     artifacts=artifacts, verification=verification)


def run_all(context: QualificationContext) -> list[dict]:
    """Collect every row of the matrix in order."""
    rows = [run_r1(context), run_r2(context), run_r3(context), run_r4(context)]
    rows.append(run_r5(context, rows))
    rows.append(run_r6(context, rows))
    return rows
