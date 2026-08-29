"""Create and verify an immutable role-to-service deployment plan."""

import hashlib
import json
from pathlib import Path


def create_plan(roles: dict, role: str, version: str) -> dict:
    if role not in roles:
        raise ValueError(f"Unsupported node role: {role}")
    definition = roles[role]
    plan = {"schema": 1, "role": role, "version": version,
            "services": definition["services"], "modules": definition["modules"],
            "capabilities": definition["capabilities"]}
    plan["sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    return plan


def verify_plan(plan: dict) -> bool:
    supplied = plan.get("sha256", "")
    unsigned = {key: value for key, value in plan.items() if key != "sha256"}
    expected = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    return bool(supplied) and supplied == expected
