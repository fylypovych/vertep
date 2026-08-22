import argparse
import os
import stat
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--template", default=".env.example")
parser.add_argument("--config", default=".env")
args = parser.parse_args()
template = Path(args.template).read_text(encoding="utf-8").splitlines()
path = Path(args.config)
mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
known = {line.split("=", 1)[0] for line in existing if "=" in line}
added = [line for line in template if "=" in line and line.split("=", 1)[0] not in known]
if added:
    path.write_text("\n".join(existing + ["", "# Added by config upgrade"] + added) + "\n", encoding="utf-8")
    os.chmod(path, mode)
print(f"Added {len(added)} missing configuration keys; existing values were preserved")
