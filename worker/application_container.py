"""Run an Application Module in its own restricted container on the node.

Application code is never imported into the CORE or Worker process. The
Worker starts the digest-pinned image from the signed manifest with a fixed,
hardened ``docker run`` argument list: no Docker socket, no host namespaces,
no bind mounts, no added capabilities, read-only root filesystem, non-root
user, one own named data volume and one own network. Secrets are not put in
the manifest or image; the module fetches its own scoped secrets from CORE with
its module token (see ``core/module/gateway.py``).
"""

from __future__ import annotations

import re
import subprocess

from core.module.manifest import ModuleManifest

UNPRIVILEGED_USER = "65532:65532"
_FORBIDDEN = (
    re.compile(r"docker\.sock"),
    re.compile(r"^--privileged"),
    re.compile(r"^--(?:network|net|pid|ipc|uts|userns|cgroupns)=host$"),
    re.compile(r"^--cap-add"),
    re.compile(r"^--device"),
    re.compile(r"^(?:-v|--volume)"),
    re.compile(r"type=bind"),
    re.compile(r"^--volumes-from"),
    re.compile(r"^--security-opt=(?:seccomp|apparmor)=unconfined"),
)


def resource_name(module_id: str) -> str:
    return "vertep-mod-" + re.sub(r"[^a-z0-9_.-]", "-", module_id.lower())


def run_arguments(manifest: ModuleManifest, *, core_url: str, module_token: str, host_port: int | None,
                  bind_address: str = "127.0.0.1", command: list[str] | None = None) -> list[str]:
    """The complete ``docker run`` argument list for one application instance."""
    if manifest.kind != "application" or not manifest.container:
        raise ValueError("Only application modules run in a module container")
    name = resource_name(manifest.module_id)
    resources = manifest.resource_requirements
    arguments = [
        "docker", "run", "--detach", "--name", name,
        "--label", f"vertep.module={manifest.module_id}",
        "--label", f"vertep.module.version={manifest.version}",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        f"--user={UNPRIVILEGED_USER}", "--pids-limit=256",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        f"--mount=type=volume,src={name}-data,dst=/data",
        f"--network={name}",
        f"--env=VERTEP_MODULE_ID={manifest.module_id}",
        f"--env=VERTEP_CORE_URL={core_url}",
        f"--env=VERTEP_MODULE_TOKEN={module_token}",
    ]
    if host_port is not None:
        # Without a host port (host executor on CORE) the module is reached only on its own network.
        arguments.insert(arguments.index(f"--network={name}") + 1,
                         f"--publish={bind_address}:{int(host_port)}:{manifest.container['port']}")
    if resources.get("min_ram_mb"):
        arguments.append(f"--memory={max(int(resources['min_ram_mb']), 64)}m")
    if resources.get("min_cpu_cores"):
        arguments.append(f"--cpus={int(resources['min_cpu_cores'])}")
    arguments.append(manifest.container["image"])
    arguments.extend(command or [])
    validate_arguments(arguments)
    return arguments


def validate_arguments(arguments: list[str]) -> None:
    """Defence in depth: reject any host escape even if a caller altered the list."""
    for argument in arguments:
        for pattern in _FORBIDDEN:
            if pattern.search(argument):
                raise ValueError(f"Forbidden container option: {argument}")
    required = {"--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", f"--user={UNPRIVILEGED_USER}"}
    if not required <= set(arguments):
        raise ValueError("Module container is missing mandatory isolation options")


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=120, check=check)


def start(manifest: ModuleManifest, **options) -> str:
    name = resource_name(manifest.module_id)
    if _docker("network", "inspect", name, check=False).returncode != 0:
        # Own bridge network per module: other modules' containers are not on it.
        _docker("network", "create", "--label", f"vertep.module={manifest.module_id}", name)
    result = subprocess.run(run_arguments(manifest, **options), capture_output=True, text=True, timeout=300, check=True)
    return result.stdout.strip()


def stop(module_id: str) -> None:
    """Stop and remove the container; the module data volume is kept (#494 owns deletion)."""
    _docker("rm", "--force", resource_name(module_id), check=False)


def running(module_id: str) -> bool:
    result = _docker("inspect", "--type", "container", "--format", "{{.State.Running}}", resource_name(module_id), check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"
