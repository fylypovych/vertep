import argparse
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser(description="Generate a local Vertep environment file")
parser.add_argument("--template", default=".env.example")
parser.add_argument("--output", default=".env")
parser.add_argument("--force", action="store_true")
args = parser.parse_args()
output = Path(args.output)
if output.exists() and not args.force:
    raise SystemExit(f"Refusing to overwrite {output}; use --force")
values = {
    "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
    "ADMIN_PASSWORD": secrets.token_urlsafe(32),
    "NODE_API_TOKEN": secrets.token_urlsafe(40),
    "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32),
}
lines = []
for line in Path(args.template).read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0] if "=" in line else ""
    lines.append(f"{key}={values[key]}" if key in values else line)
output.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.chmod(output, 0o600)
print(f"Generated {output} with unique local secrets")
