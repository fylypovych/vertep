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
        plan = planner.create_plan(Path("config/node_roles.json"), role, "1.2.3")
        assert planner.verify_plan(plan)
        assert plan["services"] == definition["services"]
    assert "postgres" not in planner.create_plan(Path("config/node_roles.json"), "gpu", "1.2.3")["services"]
    assert "worker" not in planner.create_plan(Path("config/node_roles.json"), "core", "1.2.3")["services"]


def test_plan_tampering_and_unknown_roles_fail():
    planner = module()
    plan = planner.create_plan(Path("config/node_roles.json"), "core", "1.2.3")
    plan["services"].append("worker")
    assert not planner.verify_plan(plan)
    with pytest.raises(ValueError):
        planner.create_plan(Path("config/node_roles.json"), "unknown", "1.2.3")
