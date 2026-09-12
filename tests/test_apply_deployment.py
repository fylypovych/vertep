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
