import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("deployment_plan", Path("scripts/deployment-plan.py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_each_role_has_an_isolated_valid_plan():
    planner = module()
    roles = json.loads(Path("config/node_roles.json").read_text())
    for role, definition in roles.items():
        plan = planner.create_plan(roles, role, "1.2.3")
        assert planner.verify_plan(plan)
        assert plan["services"] == definition["services"]
    assert "postgres" not in planner.create_plan(roles, "gpu", "1.2.3")["services"]
    assert "worker" not in planner.create_plan(roles, "core", "1.2.3")["services"]


def test_plan_tampering_and_unknown_roles_fail():
    planner = module()
    roles = json.loads(Path("config/node_roles.json").read_text())
    plan = planner.create_plan(roles, "core", "1.2.3")
    plan["services"].append("worker")
    assert not planner.verify_plan(plan)
    with pytest.raises(ValueError):
        planner.create_plan(roles, "unknown", "1.2.3")


def test_update_agent_has_no_docker_socket():
    compose = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose
    assert "no-new-privileges:true" in compose
    proxy = Path("deploy/proxy.conf").read_text(encoding="utf-8")
    assert "ssl_crl /etc/vertep/revocation/node-ca.crl;" in proxy
    assert "nginx -s reload" in compose


def test_appliance_images_can_be_pinned_by_signed_contract():
    compose = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    for variable in ("VERTEP_PROXY_IMAGE", "VERTEP_CORE_IMAGE", "VERTEP_WORKER_IMAGE",
                     "VERTEP_COMFYUI_IMAGE", "VERTEP_TTS_IMAGE", "VERTEP_PUBLISHER_WORKER_IMAGE",
                     "VERTEP_BACKUP_SERVICE_IMAGE", "VERTEP_POSTGRES_IMAGE", "VERTEP_REDIS_IMAGE",
                     "VERTEP_OLLAMA_IMAGE", "VERTEP_MONITORING_IMAGE", "VERTEP_GRAFANA_IMAGE",
                     "VERTEP_LOG_STORE_IMAGE", "VERTEP_LOG_COLLECTOR_IMAGE",
                     "VERTEP_UPDATE_AGENT_IMAGE", "VERTEP_LICENSE_MANAGER_IMAGE",
                     "VERTEP_DISPATCHER_IMAGE", "VERTEP_SCHEDULER_IMAGE",
                     "VERTEP_CERTIFICATE_MANAGER_IMAGE"):
        assert "${" + variable in compose
