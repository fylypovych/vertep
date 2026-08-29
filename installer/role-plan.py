import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser(description="Resolve a Vertep installer role from manifest.json")
parser.add_argument("manifest")
parser.add_argument("role", help="Role or profile declared in the manifest")
parser.add_argument("field", choices=["packages", "services"])
args = parser.parse_args()

manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
role_definitions = manifest.get("roles", {})
profiles = manifest.get("profiles", {})
if args.role in profiles:
    roles = profiles[args.role].get("includes", [])
elif args.role in role_definitions:
    roles = [args.role]
else:
    parser.error(f"unknown role/profile: {args.role}")
unknown = [role for role in roles if role not in role_definitions]
if unknown:
    parser.error(f"profile references unknown role(s): {', '.join(unknown)}")
values = []
for role in roles:
    for value in role_definitions[role].get(args.field, []):
        if value not in values:
            values.append(value)
print("\n".join(values))
