"""Create and verify an immutable role-to-service deployment plan."""

import hashlib
import json
from pathlib import Path


#: Optional engine runtimes that run beside the roles of a Core (#456). An external engine
#: is never executed inside CORE (Issue #122 P5): its assembly is a Worker task, so the
#: runtime brings the local Worker with the ``video_assembly`` capability and task.
ENGINE_RUNTIMES = {"moneyprinter": {"services": ("moneyprinter", "worker"),
                                    "capabilities": ("video_assembly",), "tasks": ("assembly",)}}
#: Services an engine runtime owns exclusively; ``worker`` is shared with the roles.
ENGINE_RUNTIME_SERVICES = frozenset({"moneyprinter"})


def create_plan(roles: dict, role: str, version: str,
                additional_roles: list[str] | None = None,
                engine_runtimes: list[str] | None = None) -> dict:
    if role not in roles:
        raise ValueError(f"Unsupported node role: {role}")
    definition = roles[role]
    if not isinstance(definition, dict) or "services" not in definition:
        raise ValueError(f"Invalid role definition: {role}")
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
    runtimes = sorted(set(engine_runtimes or []))
    if runtimes:
        if role != "core" or any(runtime not in ENGINE_RUNTIMES for runtime in runtimes):
            raise ValueError("Unsupported engine runtime")
        plan["engine_runtimes"] = runtimes
        plan["services"] = sorted(set(plan["services"]) | {service for runtime in runtimes
                                                            for service in ENGINE_RUNTIMES[runtime]["services"]})
        plan["capabilities"] = sorted(set(plan["capabilities"]) | {capability for runtime in runtimes
                                      for capability in ENGINE_RUNTIMES[runtime]["capabilities"]})
    plan["sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    return plan


def verify_plan(plan: dict) -> bool:
    supplied = plan.get("sha256", "")
    unsigned = {key: value for key, value in plan.items() if key != "sha256"}
    expected = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    return bool(supplied) and supplied == expected
