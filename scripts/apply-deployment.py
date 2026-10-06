#!/usr/bin/env python3
"""Застосування вибраної у Web Wizard ролі на appliance-хості."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.deployment_plan import create_plan
from core.persistent_data import ensure_persistent_user_data_legacy_migration


BOOTSTRAP_SERVICES = {"proxy", "core", "license-manager", "dispatcher", "scheduler",
                      "certificate-manager", "migrate", "postgres", "redis", "update-agent"}
SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
SAFE_MODEL = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}")
ROLE_TASKS = {"gpu": {"image", "video"}, "text": {"text"}, "voice": {"voice"},
              "publisher": {"publish"}, "backup": {"backup"}}


def env_value(value: object, name: str) -> str:
    if not isinstance(value, str) or any(character in value for character in "\r\n\0"):
        raise ValueError(f"Некоректне значення {name}")
    return value


def current_version(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERTEP_VERSION="):
            return line.split("=", 1)[1]
    raise ValueError("У .env відсутня встановлена версія Vertep")


def environment_value(path: Path, name: str, default: str = "") -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    return default


def update_env(path: Path, values: dict[str, str]) -> None:
    values = {key: env_value(value, key) for key, value in values.items()}
    rows, seen = [], set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
            if key in values:
                rows.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                rows.append(line)
    rows.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)


def runtime_inventory(root: Path, compose: list[str], selected: set[str], role: str,
                      version: str, runner=subprocess.run) -> dict:
    result = runner([*compose, "ps", "--all", "--format", "json"], check=True, timeout=120,
                    capture_output=True, text=True)
    rows = []
    output = getattr(result, "stdout", "") or ""
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("Service") in selected:
            digest = None
            container_id = item.get("ID")
            if container_id:
                inspected = runner(["docker", "inspect", "-f", "{{.Image}}", container_id],
                                   check=True, timeout=30, capture_output=True, text=True)
                digest = (getattr(inspected, "stdout", "") or "").strip() or None
            rows.append({"service": item.get("Service"), "image": item.get("Image"),
                         "image_digest": digest, "state": item.get("State"),
                         "health": item.get("Health") or ""})
    inventory = {"schema": 1, "version": version, "role": role,
                 "generated_at": datetime.now(timezone.utc).isoformat(),
                 "services": sorted(selected), "containers": rows}
    atomic_json(root / "config/runtime-inventory.json", inventory)
    return inventory


def compose_rows(compose: list[str], selected: set[str], runner=subprocess.run) -> list[dict]:
    """Return the raw compose inventory rows for the selected services."""
    result = runner([*compose, "ps", "--all", "--format", "json"], check=True, timeout=120,
                    capture_output=True, text=True)
    rows = []
    output = getattr(result, "stdout", "") or ""
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("Service") in selected:
            rows.append(item)
    return rows


def assert_migrate_evidence(rows: list[dict]) -> None:
    """Require a present ``migrate`` record before a deployment may be called healthy.

    An absent ``migrate`` container is *not* evidence of success: the migration
    either never ran or its record was lost.  A present record is then judged on
    its exit state by the regular health checks.
    """
    if not any(item.get("Service") == "migrate" for item in rows):
        raise RuntimeError("Migration evidence is missing: the 'migrate' service did not run")


def wait_for_healthy(compose: list[str], selected: set[str], runner=subprocess.run,
                     timeout_seconds: int = 600) -> None:
    deadline = time.monotonic() + timeout_seconds
    required_running = selected - {"migrate"}
    migrate_confirmed = False
    while True:
        rows = compose_rows(compose, selected, runner)
        if not rows:
            raise RuntimeError("Compose inventory is empty or malformed")
        found_services = {item.get("Service") for item in rows}
        missing = required_running - found_services
        if missing:
            raise RuntimeError("Missing services in inventory: " + ", ".join(sorted(missing)))
        if "migrate" in selected and not migrate_confirmed:
            # Fail-closed: no successful migration record means the deployment
            # is not proven, regardless of how healthy the long-running services
            # look.  Roles that do not run migrations (GPU/Text/Voice) are not
            # required to publish one.
            assert_migrate_evidence(rows)
            migrate_confirmed = True
        ready = {item.get("Service") for item in rows
                 if ((item.get("State") == "running" and item.get("Health") == "healthy")
                     or (item.get("Service") == "migrate" and item.get("State") == "exited" and item.get("ExitCode", 0) == 0))}
        failed = [item.get("Service") for item in rows
                  if item.get("Health") == "unhealthy"
                  or (item.get("State") == "exited" and item.get("Service") != "migrate")
                  or (item.get("Service") == "migrate" and item.get("State") == "exited" and item.get("ExitCode", 0) != 0)]
        if failed:
            raise RuntimeError("Selected services failed health checks: " + ", ".join(sorted(failed)))
        if required_running <= ready:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Selected services did not become healthy before timeout")
        time.sleep(5)


def _restore_runtime(compose: list[str], previous_services: set[str], new_services: set[str],
                     runner=subprocess.run) -> dict:
    """Restore the previously-working service/runtime set after a partial compose apply.

    On a failed deployment the caller has already restored the ``.env`` and
    ``deployment-plan.json`` configuration, but the containers may have been
    partially started, stopped or replaced for the *new* service set.  This
    helper brings the previous services back up and removes any service that was
    only introduced by the failed apply, so the appliance returns to the last
    known-good runtime instead of a half-applied one.  Rollback failures must
    never mask the original deployment error.

    The returned record always describes what was attempted and whether the
    restored runtime set was explicitly re-verified healthy *and* confirmed to
    match the previous set.  It never raises: the caller decides how to surface
    rollback problems alongside the original deployment failure.
    """
    superseded = sorted(new_services - previous_services)
    result = {"restored": False, "health_verified": False, "runtime_set_verified": False,
              "expected_services": sorted(previous_services),
              "observed_services": [], "missing_services": [],
              "restarted": sorted(previous_services), "removed": superseded, "errors": []}
    try:
        if previous_services:
            runner([*compose, "up", "-d", *sorted(previous_services)],
                   check=False, timeout=900)
        # Superseded services are removed *before* verification so the observed
        # runtime set can be compared against the previous one.
        if superseded:
            runner([*compose, "stop", *superseded], check=False, timeout=600)
            runner([*compose, "rm", "-f", *superseded], check=False, timeout=600)
        if not previous_services:
            # Nothing was running before the failed apply: the honest proof is
            # that the leftover new set is gone, not a health check.
            observed = {item.get("Service") for item in compose_rows(compose, set(new_services), runner)}
            result["observed_services"] = sorted(observed)
            still_running = sorted(observed & set(superseded))
            result["runtime_set_verified"] = not still_running
            if still_running:
                raise RuntimeError("Rollback left services running: " + ", ".join(still_running))
            result["health_verified"] = True
            result["restored"] = True
            return result
        observed = {item.get("Service") for item in compose_rows(compose, set(previous_services), runner)}
        result["observed_services"] = sorted(observed)
        result["missing_services"] = sorted(previous_services - observed)
        if result["missing_services"]:
            raise RuntimeError("Restored runtime set is incomplete: "
                               + ", ".join(result["missing_services"]))
        wait_for_healthy(compose, previous_services, runner, timeout_seconds=120)
        result["health_verified"] = True
        result["runtime_set_verified"] = True
        result["restored"] = True
    except Exception as error:
        result["errors"].append(str(error))
    return result


def _module_status(containers: list[dict], modules: list[str]) -> dict:
    """Derive per-module status from the observed containers, never from the plan.

    A module is ``DEGRADED`` when the service backing it is not running/healthy,
    or when any observed container of the deployment is unhealthy.  It is
    ``HEALTHY`` when its own service is running and healthy, or when the whole
    observed runtime is healthy.  A module with no observed evidence is reported
    as ``UNKNOWN`` instead of a fabricated ``HEALTHY``.
    """
    running = {item.get("service") for item in containers
               if item.get("state") == "running" and item.get("health") in {"", "healthy"}}
    degraded = {item.get("service") for item in containers
                if item.get("health") == "unhealthy"
                or (item.get("state") != "running" and item.get("service") != "migrate")}
    runtime_ok = bool(running) and not degraded
    status = {}
    for module in modules:
        if module in degraded:
            status[module] = "DEGRADED"
        elif module in running or runtime_ok:
            status[module] = "HEALTHY"
        else:
            status[module] = "UNKNOWN"
    return status


def _refresh_inventory_after_rollback(root: Path, compose: list[str], selected: set[str],
                                      role: str, version: str, runner=subprocess.run) -> None:
    """Rewrite runtime-inventory.json from the runtime that is actually present."""
    # Probe every service involved in the apply/rollback so the inventory
    # describes the real runtime set instead of the failed new plan.
    rows = compose_rows(compose, selected, runner)
    containers = [{"service": item.get("Service"), "image": item.get("Image"),
                   "state": item.get("State"), "health": item.get("Health") or ""}
                  for item in rows]
    previous = sorted(item.get("service") for item in containers if item.get("service"))
    inventory = {"schema": 1, "version": version, "role": role,
                 "generated_at": datetime.now(timezone.utc).isoformat(),
                 "services": previous, "containers": containers,
                 "runtime_set_verified": True, "after_rollback": True}
    atomic_json(root / "config/runtime-inventory.json", inventory)
    installation_path = root / "config/installation.json"
    if installation_path.is_file():
        installation = json.loads(installation_path.read_text(encoding="utf-8"))
        installation["runtime"] = inventory
        atomic_json(installation_path, installation)


def apply(root: Path, runner=subprocess.run) -> dict:
    request_path = root / "config/deployment-request.json"
    original_request = request_path.read_bytes()
    try:
        return _apply(root, runner)
    except Exception:
        # PathExists otherwise immediately retries the failed request, repeatedly
        # applying and rolling back the same runtime. Preserve it for diagnosis,
        # without consuming a replacement request submitted during this attempt.
        if request_path.is_file() and request_path.read_bytes() == original_request:
            failed = root / "config/deployment-failed"
            failed.mkdir(parents=True, exist_ok=True)
            request_path.replace(failed / f"request-{time.time_ns()}.json")
        raise


def _apply(root: Path, runner=subprocess.run) -> dict:
    request_path = root / "config/deployment-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
    role = request.get("role")
    version = request.get("version")
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise ValueError("Deployment request містить некоректну версію")
    if version != current_version(root / ".env"):
        raise RuntimeError("Deployment request створено для іншої версії Vertep")
    additional_roles = request.get("additional_roles", [])
    if not isinstance(additional_roles, list) or any(not isinstance(item, str) for item in additional_roles):
        raise ValueError("Некоректний список додаткових ролей")
    plan = create_plan(roles, role, version, additional_roles)
    if request.get("plan_sha256") != plan["sha256"]:
        raise RuntimeError("Deployment request не відповідає підписаному каталогу ролей")
    core_url = request.get("core_url") or ""
    if role != "core":
        core_url = env_value(core_url, "CORE_URL").rstrip("/")
        parsed = urlsplit(core_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Non-Core роль потребує коректний HTTPS Core URL")
    model = request.get("ollama_model", "llama3.2")
    managed_ollama = request.get("ai_backend") == "ollama" and "ollama" in plan["services"]
    if managed_ollama and (not isinstance(model, str) or not SAFE_MODEL.fullmatch(model)):
        raise ValueError("Некоректна назва Ollama model")

    env_path = root / ".env"
    plan_path = root / "config/deployment-plan.json"
    previous_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    previous_plan = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
    previous_services = set()
    if plan_path.exists():
        try:
            previous_services = set(json.loads(plan_path.read_text(encoding="utf-8")).get("services", []))
        except (OSError, ValueError):
            previous_services = set()
    new_services = set(plan["services"])

    def compose_command() -> list[str]:
        command = ["docker", "compose", "--env-file", str(root / ".env"),
                   "-f", str(root / "docker-compose.yml")]
        gpu_vendor = environment_value(root / ".env", "GPU_VENDOR")
        if gpu_vendor == "amd" and (root / "docker-compose.amd.yml").is_file():
            command.extend(["-f", str(root / "docker-compose.amd.yml")])
        elif gpu_vendor == "nvidia" and (root / "docker-compose.nvidia.yml").is_file():
            command.extend(["-f", str(root / "docker-compose.nvidia.yml")])
        return command

    def restore() -> dict:
        if previous_env:
            env_path.write_text(previous_env, encoding="utf-8")
        else:
            env_path.unlink(missing_ok=True)
        if previous_plan:
            plan_path.write_text(previous_plan, encoding="utf-8")
        else:
            plan_path.unlink(missing_ok=True)
        # Migrate legacy ephemeral roots from old container filesystem before
        # restarting the previous runtime set. This ensures user data created
        # in /app/{characters,brands,workflows} during a previous version is
        # copied to persistent storage before the old containers are recreated.
        try:
            ensure_persistent_user_data_legacy_migration()
        except Exception as error:  # noqa: BLE001 - never block restore on migration
            print(f"WARNING: legacy migration failed: {error}", file=sys.stderr)
        # A failed apply may have partially started/stopped the new service set;
        # restore the previously-working runtime so the host is not left half-applied.
        return _restore_runtime(compose_command(), previous_services, new_services, runner)

    update_env(env_path, {
        "NODE_ROLE": role,
        "NODE_ADDITIONAL_ROLES": ",".join(plan.get("additional_roles", [])),
        "NODE_CAPABILITIES": ",".join(sorted({capability
                                               for selected_role in ([role] if role != "core" else additional_roles)
                                               for capability in roles[selected_role]["capabilities"]})),
        "SUPPORTED_TASKS": ",".join(sorted({task for selected_role in ([role] if role != "core" else additional_roles)
                                                   for task in ROLE_TASKS.get(selected_role, set())})),
        "WORKER_REQUIRE_GPU": "false" if role == "core" else
                              ("true" if role == "gpu" else "false"),
        "CORE_URL": core_url,
        "CORE_ADDRESS": core_url,
        "REGISTRATION_TOKEN": "",
    })
    atomic_json(plan_path, plan)
    compose = compose_command()
    selected = set(plan["services"])
    all_services = BOOTSTRAP_SERVICES | {service for definition in roles.values()
                                        if isinstance(definition, dict)
                                        for service in definition["services"]}
    unwanted = sorted(all_services - selected)
    status = {"state": "APPLYING", "role": role,
              "additional_roles": plan.get("additional_roles", []), "services": sorted(selected),
              "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(root / "config/deployment-status.json", status)
    try:
        runner([*compose, "pull", *sorted(selected)], check=True, timeout=3600)
        runner([*compose, "up", "-d", "--remove-orphans", *sorted(selected)],
               check=True, timeout=1800)
        if managed_ollama:
            wait_for_healthy(compose, {"ollama"}, runner)
            runner([*compose, "exec", "-T", "ollama", "ollama", "pull", model],
                   check=True, timeout=3600)
        wait_for_healthy(compose, selected, runner)
        inventory = runtime_inventory(root, compose, selected, role, version, runner)
        # Set-equality: the observed runtime set must match the intended plan.
        observed = {item["service"] for item in inventory["containers"]}
        missing = selected - observed
        if missing:
            raise RuntimeError("Runtime set does not match the plan; missing services: "
                               + ", ".join(sorted(missing)))
        inventory["missing_services"] = sorted(missing)
        inventory["runtime_set_verified"] = True
        unhealthy = [item["service"] for item in inventory["containers"]
                     if ((item["state"] != "running" and item["service"] != "migrate")
                         or item["health"] not in {"", "healthy"})]
        if unhealthy:
            raise RuntimeError("Selected services are not healthy: " + ", ".join(unhealthy))
        inventory["modules"] = _module_status(inventory["containers"], plan["modules"])
        atomic_json(root / "config/runtime-inventory.json", inventory)
        installation_path = root / "config/installation.json"
        if installation_path.is_file():
            installation = json.loads(installation_path.read_text(encoding="utf-8"))
            installation["runtime"] = inventory
            atomic_json(installation_path, installation)
            public_manifest = {key: value for key, value in installation.items()
                               if key != "administrator"}
            public_manifest["administrator"] = {
                "username": installation.get("administrator", {}).get("username"), "role": "admin"}
            public_manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
                {key: value for key, value in installation.items() if key != "administrator"},
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            atomic_json(root / "config/installation-manifest.json", public_manifest)
        if unwanted:
            runner([*compose, "stop", *unwanted], check=True, timeout=600)
            runner([*compose, "rm", "-f", *unwanted], check=True, timeout=600)
        # Migrate legacy ephemeral roots from old container filesystem after
        # successful apply, so user data from previous versions is preserved.
        try:
            ensure_persistent_user_data_legacy_migration()
        except Exception as error:  # noqa: BLE001
            print(f"WARNING: legacy migration failed: {error}", file=sys.stderr)
        status.update({"state": "SUCCEEDED", "updated_at": datetime.now(timezone.utc).isoformat(),
                       "runtime_set_verified": True, "observed_services": sorted(observed)})
        atomic_json(root / "config/deployment-status.json", status)
        request_path.unlink()
        return status
    except Exception as error:
        rollback_result = restore()
        # The on-disk inventory still describes the failed new service set, so
        # it is regenerated from the restored runtime instead of being left to
        # claim services that no longer exist.
        if rollback_result.get("runtime_set_verified"):
            try:
                _refresh_inventory_after_rollback(
                    root, compose, selected | set(rollback_result.get("expected_services", [])),
                    role, version, runner)
                rollback_result["inventory_refreshed"] = True
            except Exception as inventory_error:  # pragma: no cover - defensive
                rollback_result["inventory_refreshed"] = False
                rollback_result["errors"].append(f"inventory refresh failed: {inventory_error}")
        status.update({"state": "FAILED", "error": str(error),
                       "updated_at": datetime.now(timezone.utc).isoformat()})
        status["rollback"] = rollback_result
        atomic_json(root / "config/deployment-status.json", status)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Застосувати роль Vertep із Web Wizard")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    apply(args.root.resolve())


if __name__ == "__main__":
    main()
