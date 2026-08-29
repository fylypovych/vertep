#!/usr/bin/env python3
"""Create and verify an immutable role-to-service deployment plan."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.deployment_plan import create_plan, verify_plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("roles", type=Path)
    parser.add_argument("role")
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    roles = json.loads(args.roles.read_text(encoding="utf-8"))
    value = create_plan(roles, args.role, args.version)
    args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
