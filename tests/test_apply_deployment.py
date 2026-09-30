import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[1] / "scripts" / "apply-deployment.py"
    spec = importlib.util.spec_from_file_location("apply_deployment", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def fixture(tmp_path, role="gpu"):
    root = tmp_path / "vertep"
    (root / "config").mkdir(parents=True)
    roles = {
        "core": {"services": ["proxy", "core", "postgres", "ollama"],
                 "capabilities": ["scheduling"], "modules": ["core"]},
        "gpu": {"services": ["worker", "comfyui", "update-agent"],
                "capabilities": ["image_generation"], "modules": ["worker", "comfyui"]},
        "text": {"services": ["worker", "ollama", "update-agent"],
                 "capabilities": ["text_generation"], "modules": ["worker", "ollama"]},
    }
    (root / "config/node_roles.json").write_text(json.dumps(roles), encoding="utf-8")
    (root / ".env").write_text(
        "VERTEP_VERSION=0.0.0.20\nNODE_ROLE=unassigned\nREGISTRATION_TOKEN=sensitive\n",
        encoding="utf-8")
    plan = module().create_plan(roles, role, "0.0.0.20")
    (root / "config/deployment-request.json").write_text(json.dumps({
        "schema": 1, "role": role, "version": "0.0.0.20", "ai_backend": "ollama",
        "core_url": None if role == "core" else "https://core.example",
        "plan_sha256": plan["sha256"], "ollama_model": "llama3.2",
    }), encoding="utf-8")
    return root


def test_apply_deployment_uses_only_catalog_services_and_erases_token(tmp_path):
    root = fixture(tmp_path)
    commands = []
    selected = {"comfyui", "update-agent", "worker"}

    def runner(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["compose", "--env-file"] and command[-3:] == ["ps", "--format", "json"]:
            rows = [json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in selected]
            class Result:
                stdout = "\n".join(rows) + "\n"
            return Result()
        return None

    result = module().apply(root, runner=runner)
    assert result["state"] == "SUCCEEDED"
    assert result["services"] == ["comfyui", "update-agent", "worker"]
    assert not (root / "config/deployment-request.json").exists()
    environment = (root / ".env").read_text(encoding="utf-8")
    assert "NODE_ROLE=gpu" in environment
    assert "CORE_URL=https://core.example" in environment
    assert "REGISTRATION_TOKEN=\n" in environment
    assert commands[0][-3:] == ["comfyui", "update-agent", "worker"]
    assert "core" in commands[-1]


def test_apply_deployment_rejects_tampered_plan(tmp_path):
    root = fixture(tmp_path)
    request = json.loads((root / "config/deployment-request.json").read_text())
    request["plan_sha256"] = "0" * 64
    (root / "config/deployment-request.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(RuntimeError, match="підписаному каталогу"):
        module().apply(root, runner=lambda *args, **kwargs: None)


def test_text_deployment_provisions_selected_model(tmp_path):
    root = fixture(tmp_path, "text")
    commands = []
    selected = {"ollama", "update-agent", "worker"}

    def runner(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["compose", "--env-file"] and command[-3:] == ["ps", "--format", "json"]:
            rows = [json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in selected]
            class Result:
                stdout = "\n".join(rows) + "\n"
            return Result()
        return None
    module().apply(root, runner=runner)
    assert any(command[-6:] == ["exec", "-T", "ollama", "ollama", "pull", "llama3.2"]
               for command in commands)


def test_core_deployment_provisions_managed_ollama_model(tmp_path):
    root = fixture(tmp_path, "core")
    commands = []
    selected = {"core", "ollama", "postgres", "proxy", "update-agent"}

    def runner(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["compose", "--env-file"] and command[-3:] == ["ps", "--format", "json"]:
            rows = [json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in selected]
            class Result:
                stdout = "\n".join(rows) + "\n"
            return Result()
        return None
    module().apply(root, runner=runner)
    assert any(command[-6:] == ["exec", "-T", "ollama", "ollama", "pull", "llama3.2"]
               for command in commands)


def test_core_deployment_activates_multiple_local_roles(tmp_path):
    root = fixture(tmp_path, "core")
    roles = json.loads((root / "config/node_roles.json").read_text())
    plan = module().create_plan(roles, "core", "0.0.0.20", ["gpu", "text"])
    request_path = root / "config/deployment-request.json"
    request = json.loads(request_path.read_text())
    request.update({"additional_roles": ["text", "gpu"], "plan_sha256": plan["sha256"]})
    request_path.write_text(json.dumps(request), encoding="utf-8")
    commands = []
    selected = {"core", "worker", "comfyui", "postgres", "proxy", "ollama", "update-agent"}

    def runner(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["compose", "--env-file"] and command[-3:] == ["ps", "--format", "json"]:
            rows = [json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in selected]
            class Result:
                stdout = "\n".join(rows) + "\n"
            return Result()
        return None
    result = module().apply(root, runner=runner)
    assert result["additional_roles"] == ["gpu", "text"]
    assert {"core", "worker", "comfyui", "ollama"} <= set(result["services"])
    environment = (root / ".env").read_text(encoding="utf-8")
    assert "NODE_ROLE=core" in environment
    assert "NODE_ADDITIONAL_ROLES=gpu,text" in environment
    assert "NODE_CAPABILITIES=image_generation,text_generation" in environment
    assert "SUPPORTED_TASKS=image,text,video" in environment


def test_apply_deployment_rejects_environment_injection(tmp_path):
    root = fixture(tmp_path)
    request_path = root / "config/deployment-request.json"
    request = json.loads(request_path.read_text())
    request["core_url"] = "https://core.example\nNODE_ROLE=core"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="CORE_URL"):
        module().apply(root, runner=lambda *args, **kwargs: None)


def test_apply_deployment_rejects_unsafe_model_name(tmp_path):
    root = fixture(tmp_path, "text")
    request_path = root / "config/deployment-request.json"
    request = json.loads(request_path.read_text())
    request["ollama_model"] = "model name"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="Ollama"):
        module().apply(root, runner=lambda *args, **kwargs: None)


def test_apply_deployment_rejects_request_for_another_version(tmp_path):
    root = fixture(tmp_path)
    request_path = root / "config/deployment-request.json"
    request = json.loads(request_path.read_text())
    request["version"] = "0.0.0.99"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(RuntimeError, match="іншої версії"):
        module().apply(root, runner=lambda *args, **kwargs: None)


def test_wait_for_healthy_fails_on_empty_inventory(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = ""
            return Result()
        return None
    with pytest.raises(RuntimeError, match="empty or malformed"):
        mod.wait_for_healthy(["docker", "compose"], {"worker"}, runner=runner)


def test_wait_for_healthy_fails_on_malformed_inventory(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = "not json\n{broken\n"
            return Result()
        return None
    with pytest.raises(RuntimeError, match="empty or malformed"):
        mod.wait_for_healthy(["docker", "compose"], {"worker"}, runner=runner)


def test_wait_for_healthy_fails_on_missing_service(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "worker", "State": "running", "Health": "healthy"}\n'
            return Result()
        return None
    with pytest.raises(RuntimeError, match="Missing services"):
        mod.wait_for_healthy(["docker", "compose"], {"worker", "comfyui"}, runner=runner)


def test_wait_for_healthy_fails_on_unhealthy_service(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "worker", "State": "running", "Health": "unhealthy"}\n'
            return Result()
        return None
    with pytest.raises(RuntimeError, match="failed health checks"):
        mod.wait_for_healthy(["docker", "compose"], {"worker"}, runner=runner)


def test_wait_for_healthy_fails_on_failed_migrate(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "migrate", "State": "exited", "ExitCode": 1}\n'
            return Result()
        return None
    with pytest.raises(RuntimeError, match="failed health checks"):
        mod.wait_for_healthy(["docker", "compose"], {"migrate"}, runner=runner)


def test_wait_for_healthy_succeeds_on_successful_migrate(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "migrate", "State": "exited", "ExitCode": 0}\n'
            return Result()
        return None
    mod.wait_for_healthy(["docker", "compose"], {"migrate"}, runner=runner)


def test_wait_for_healthy_fails_when_migrate_evidence_is_absent(tmp_path):
    """M6: an absent migrate record is not migration evidence and must not pass."""
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = ('{"Service": "core", "State": "running", "Health": "healthy"}\n'
                          '{"Service": "postgres", "State": "running", "Health": "healthy"}\n')
            return Result()
        return None
    with pytest.raises(RuntimeError, match="Migration evidence is missing"):
        mod.wait_for_healthy(["docker", "compose"], {"core", "postgres", "migrate"},
                             runner=runner, timeout_seconds=0)


def test_wait_for_healthy_without_migrate_service_does_not_require_evidence(tmp_path):
    """A role that runs no migrations (GPU/Text/Voice) is not blocked by the gate."""
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = ('{"Service": "worker", "State": "running", "Health": "healthy"}\n'
                          '{"Service": "comfyui", "State": "running", "Health": "healthy"}\n')
            return Result()
        return None
    mod.wait_for_healthy(["docker", "compose"], {"worker", "comfyui"}, runner=runner,
                         timeout_seconds=0)


def test_apply_deployment_rolls_back_env_and_plan_on_failure(tmp_path):
    root = fixture(tmp_path)
    original_env = (root / ".env").read_text(encoding="utf-8")
    mod = module()
    # First create a valid deployment-plan.json
    plan = mod.create_plan(json.loads((root / "config/node_roles.json").read_text()), "gpu", "0.0.0.20")
    (root / "config/deployment-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    original_plan = (root / "config/deployment-plan.json").read_text(encoding="utf-8")

    call_count = {"wait_for_healthy": 0}
    def runner(command, **kwargs):
        if "ps" in command:
            call_count["wait_for_healthy"] += 1
            # Simulate worker unhealthy among selected services
            class Result:
                stdout = ('{"Service": "comfyui", "State": "running", "Health": "healthy"}\n'
                          '{"Service": "update-agent", "State": "running", "Health": "healthy"}\n'
                          '{"Service": "worker", "State": "running", "Health": "unhealthy"}\n')
            return Result()
        return None

    with pytest.raises(RuntimeError, match="health"):
        mod.apply(root, runner=runner)

    # Check rollback restored .env and deployment-plan.json
    assert (root / ".env").read_text(encoding="utf-8") == original_env
    assert (root / "config/deployment-plan.json").read_text(encoding="utf-8") == original_plan
    # Check deployment-status.json has FAILED state with error
    status = json.loads((root / "config/deployment-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED"
    assert "error" in status
    assert "updated_at" in status


def test_apply_deployment_preserves_status_progress_result_on_failure(tmp_path):
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "worker", "State": "running", "Health": "unhealthy"}\n'
            return Result()
        return None

    with pytest.raises(RuntimeError):
        mod.apply(root, runner=runner)

    status = json.loads((root / "config/deployment-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED"
    assert "error" in status
    assert "updated_at" in status
    assert status["role"] == "gpu"
    assert "services" in status
    assert "additional_roles" in status
def test_restore_runtime_reports_unverified_health_when_wait_fails(tmp_path):
    """M6: a rollback whose restarted set never becomes healthy must not claim success."""
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "ollama", "State": "running", "Health": "unhealthy"}\n'
            return Result()
        return None
    result = mod._restore_runtime(["docker", "compose"], {"ollama"}, {"comfyui"}, runner=runner)
    assert result["restored"] is False
    assert result["health_verified"] is False
    assert result["restarted"] == ["ollama"]
    assert result["removed"] == ["comfyui"]
    assert result["errors"]


def test_restore_runtime_reports_verified_health_on_success(tmp_path):
    """M6: a rollback that re-verifies the previous set healthy is explicitly attested."""
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "ollama", "State": "running", "Health": "healthy"}\n'
            return Result()
        return None
    result = mod._restore_runtime(["docker", "compose"], {"ollama"}, {"comfyui"}, runner=runner)
    assert result["restored"] is True
    assert result["health_verified"] is True
    assert result["restarted"] == ["ollama"]
    assert result["removed"] == ["comfyui"]
    assert not result["errors"]


def test_failed_apply_records_explicit_rollback_attestation(tmp_path):
    """M6: deployment-status.json records whether the rollback health was verified."""
    root = fixture(tmp_path)
    mod = module()
    roles = json.loads((root / "config/node_roles.json").read_text())
    previous = mod.create_plan(roles, "text", "0.0.0.20")
    (root / "config/deployment-plan.json").write_text(json.dumps(previous), encoding="utf-8")
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "worker", "State": "running", "Health": "unhealthy"}\n'
            return Result()
        return None

    with pytest.raises(RuntimeError):
        mod.apply(root, runner=runner)

    status = json.loads((root / "config/deployment-status.json").read_text(encoding="utf-8"))
    assert "rollback" in status
    assert status["rollback"]["health_verified"] is False
    assert status["rollback"]["restarted"] == sorted(previous["services"])
    assert status["rollback"]["removed"] == sorted(
        set(mod.create_plan(roles, "gpu", "0.0.0.20")["services"]) - set(previous["services"]))


def test_wait_for_healthy_rejects_missing_health_evidence(tmp_path):
    """A running container with no health data must not count as healthy."""
    root = fixture(tmp_path)
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "worker", "State": "running", "Health": ""}\n'
            return Result()
        return None
    # Missing Health evidence is neither ready nor failed -> only a timeout can
    # resolve it, proving that absent evidence does not shortcut to success.
    with pytest.raises(RuntimeError, match="timeout"):
        mod.wait_for_healthy(["docker", "compose"], {"worker"}, runner=runner, timeout_seconds=0)


def test_apply_rolls_back_previous_runtime_set_after_partial_compose(tmp_path):
    """A failed apply re-starts the previous service set and removes superseded services."""
    root = fixture(tmp_path)
    mod = module()
    roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
    previous = mod.create_plan(roles, "text", "0.0.0.20")
    (root / "config/deployment-plan.json").write_text(json.dumps(previous), encoding="utf-8")

    recorded = []
    def runner(command, **kwargs):
        recorded.append(command)
        if "ps" in command:
            class Result:
                # If checking rollback services (ollama, update-agent, worker) return healthy
                stdout = ('{"Service": "ollama", "State": "running", "Health": "healthy"}\n'
                          '{"Service": "comfyui", "State": "running", "Health": "healthy"}\n'
                          '{"Service": "update-agent", "State": "running", "Health": "healthy"}\n'
                          + json.dumps({"Service": "worker", "State": "running",
                                        "Health": "healthy" if any("up" in c and "ollama" in c for c in recorded) else "unhealthy"}) + '\n')
            return Result()
        return None

    with pytest.raises(RuntimeError, match="health"):
        mod.apply(root, runner=runner)

    # Configuration restored back to the previous ("text") role.
    assert "NODE_ROLE=gpu" not in (root / ".env").read_text(encoding="utf-8")
    restored = json.loads((root / "config/deployment-plan.json").read_text(encoding="utf-8"))
    assert restored["role"] == "text"

    up_commands = [c for c in recorded if c[0] == "docker" and c[1] == "compose" and "up" in c]
    # Rollback re-started the previous set, including a service not in the new gpu role.
    assert any("ollama" in c and "comfyui" not in c for c in up_commands)
    # The service introduced only by the failed gpu apply (comfyui) was torn down.
    assert any("stop" in c and "comfyui" in c for c in recorded)
    assert any("rm" in c and "-f" in c and "comfyui" in c for c in recorded)


def test_restore_runtime_verifies_set_with_no_previous_services(tmp_path):
    """M6: a first-ever deployment rollback must still prove the leftover set is gone."""
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = ""
            return Result()
        return None
    result = mod._restore_runtime(["docker", "compose"], set(), {"comfyui"}, runner=runner)
    assert result["restored"] is True
    assert result["health_verified"] is True
    assert result["runtime_set_verified"] is True
    assert result["observed_services"] == []
    assert not result["errors"]


def test_restore_runtime_fails_when_superseded_service_survives(tmp_path):
    """M6: a leftover service from the failed apply must block a restored claim."""
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "comfyui", "State": "running", "Health": "healthy"}\n'
            return Result()
        return None
    result = mod._restore_runtime(["docker", "compose"], set(), {"comfyui"}, runner=runner)
    assert result["restored"] is False
    assert result["runtime_set_verified"] is False
    assert result["errors"]


def test_restore_runtime_reports_incomplete_restored_set(tmp_path):
    """M6: restored health that does not cover the previous set is not attested."""
    mod = module()
    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = '{"Service": "ollama", "State": "running", "Health": "healthy"}\n'
            return Result()
        return None
    result = mod._restore_runtime(["docker", "compose"], {"ollama", "worker"}, {"comfyui"},
                                  runner=runner)
    assert result["restored"] is False
    assert result["runtime_set_verified"] is False
    assert "worker" in result["missing_services"]


def test_apply_fails_when_observed_runtime_set_differs_from_plan(tmp_path):
    """M6: a partially applied runtime set must not be reported as SUCCEEDED."""
    root = fixture(tmp_path)
    mod = module()
    selected = {"comfyui", "update-agent", "worker"}

    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                # comfyui never appears: the runtime set does not match the plan.
                stdout = "\n".join(
                    json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in selected - {"comfyui"}) + "\n"
            return Result()
        return None

    with pytest.raises(RuntimeError, match="Missing services"):
        mod.apply(root, runner=runner)
    status = json.loads((root / "config/deployment-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED"


def test_apply_records_verified_runtime_set_on_success(tmp_path):
    """M6: a successful apply attests the observed runtime set explicitly."""
    root = fixture(tmp_path)
    mod = module()
    selected = {"comfyui", "update-agent", "worker"}

    def runner(command, **kwargs):
        if "ps" in command:
            class Result:
                stdout = "\n".join(
                    json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in selected) + "\n"
            return Result()
        return None

    result = mod.apply(root, runner=runner)
    assert result["runtime_set_verified"] is True
    assert result["observed_services"] == sorted(selected)
    inventory = json.loads((root / "config/runtime-inventory.json").read_text(encoding="utf-8"))
    assert inventory["runtime_set_verified"] is True
    assert inventory["missing_services"] == []


def test_module_status_is_derived_from_observed_containers(tmp_path):
    """M6: module status must be measured, never a fabricated HEALTHY constant."""
    mod = module()
    containers = [{"service": "worker", "state": "running", "health": "healthy"},
                  {"service": "comfyui", "state": "exited", "health": ""}]
    status = mod._module_status(containers, ["worker", "comfyui", "web_ui"])
    assert status["worker"] == "HEALTHY"
    assert status["comfyui"] == "DEGRADED"
    # A module with no observed container is UNKNOWN, not a fabricated HEALTHY.
    assert status["web_ui"] == "UNKNOWN"
    assert mod._module_status([], ["worker"]) == {"worker": "UNKNOWN"}
    healthy = mod._module_status([{"service": "worker", "state": "running", "health": "healthy"}],
                                 ["worker", "comfyui"])
    assert healthy == {"worker": "HEALTHY", "comfyui": "HEALTHY"}


def test_apply_refreshes_inventory_after_rollback(tmp_path):
    """M6: after a rollback the inventory must describe the restored runtime."""
    root = fixture(tmp_path)
    mod = module()
    roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
    previous = mod.create_plan(roles, "text", "0.0.0.20")
    (root / "config/deployment-plan.json").write_text(json.dumps(previous), encoding="utf-8")
    restored_services = set(previous["services"])

    def runner(command, **kwargs):
        if "ps" in command:
            rows = [json.dumps({"Service": s, "State": "running", "Health": "healthy"})
                    for s in sorted(restored_services)]
            class Result:
                stdout = "\n".join(rows) + "\n"
            return Result()
        return None

    with pytest.raises(RuntimeError):
        mod.apply(root, runner=runner)

    inventory = json.loads((root / "config/runtime-inventory.json").read_text(encoding="utf-8"))
    assert inventory.get("after_rollback") is True
    assert set(inventory["services"]) == restored_services
    status = json.loads((root / "config/deployment-status.json").read_text(encoding="utf-8"))
    assert status["rollback"]["runtime_set_verified"] is True
    assert status["rollback"]["inventory_refreshed"] is True
