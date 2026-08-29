import argparse
import os
import stat
from pathlib import Path


parser = argparse.ArgumentParser(description="Set one key in a dotenv file without printing its value")
parser.add_argument("path")
parser.add_argument("key")
parser.add_argument("value")
args = parser.parse_args()

path = Path(args.path)
mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
replacement = f"{args.key}={args.value}"
updated = []
found = False
for line in lines:
    if line.split("=", 1)[0] == args.key:
        if not found:
            updated.append(replacement)
            found = True
    else:
        updated.append(line)
if not found:
    updated.append(replacement)
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
os.chmod(temporary, mode)
temporary.replace(path)
