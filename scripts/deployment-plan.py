#!/usr/bin/env python3
"""Create and verify an immutable role-to-service deployment plan."""

import argparse
import hashlib
import json
from pathlib import Path


def create_plan(roles_path: Path, role: str, version: str) -> dict:
    roles = json.loads(roles_path.read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("roles", type=Path)
    parser.add_argument("role")
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = create_plan(args.roles, args.role, args.version)
    args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
