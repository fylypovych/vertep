"""Create and verify an immutable role-to-service deployment plan."""

import hashlib
import json
from pathlib import Path


def create_plan(roles: dict, role: str, version: str,
                additional_roles: list[str] | None = None) -> dict:
    if role not in roles:
        raise ValueError(f"Unsupported node role: {role}")
    extras = sorted(set(additional_roles or []))
    if extras and role != "core":
        raise ValueError("Additional roles can only be activated on Core")
    if role in extras or any(extra not in roles or extra == "core" for extra in extras):
        raise ValueError("Unsupported additional node role")
    definitions = [roles[role], *(roles[extra] for extra in extras)]
    definition = roles[role]
    plan = {"schema": 1, "role": role, "version": version,
            "services": (definition["services"] if not extras else
                         sorted({item for value in definitions for item in value["services"]})),
            "modules": (definition["modules"] if not extras else
                        sorted({item for value in definitions for item in value["modules"]})),
            "capabilities": (definition["capabilities"] if not extras else
                             sorted({item for value in definitions for item in value["capabilities"]}))}
    if extras:
        plan["additional_roles"] = extras
    plan["sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    return plan


def verify_plan(plan: dict) -> bool:
    supplied = plan.get("sha256", "")
    unsigned = {key: value for key, value in plan.items() if key != "sha256"}
    expected = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    return bool(supplied) and supplied == expected
