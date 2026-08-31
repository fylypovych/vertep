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

    def runner(command, **kwargs):
        commands.append(command)

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
    module().apply(root, runner=lambda command, **kwargs: commands.append(command))
    assert any(command[-6:] == ["exec", "-T", "ollama", "ollama", "pull", "llama3.2"]
               for command in commands)


def test_core_deployment_provisions_managed_ollama_model(tmp_path):
    root = fixture(tmp_path, "core")
    commands = []
    module().apply(root, runner=lambda command, **kwargs: commands.append(command))
    assert any(command[-6:] == ["exec", "-T", "ollama", "ollama", "pull", "llama3.2"]
               for command in commands)


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
