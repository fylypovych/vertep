import argparse
import base64
import json
import os
import urllib.request
import sys
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "backup":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.backup import main
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    raise SystemExit(main())

parser = argparse.ArgumentParser(prog="vertep-cli")
parser.add_argument("--url", default=os.getenv("VERTEP_CORE_URL", "http://127.0.0.1:8080"))
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("jobs")
create = sub.add_parser("create"); create.add_argument("topic"); create.add_argument("--character", default="did_samogon")
action = sub.add_parser("action"); action.add_argument("job_id"); action.add_argument("action", choices=["pause","resume","retry","regenerate","cancel","approve","publish"])
sub.add_parser("workers"); sub.add_parser("characters"); sub.add_parser("workflows"); sub.add_parser("metrics")
module = sub.add_parser("module")
module.add_argument("operation", choices=["install", "uninstall", "list", "info"])
module.add_argument("target", nargs="?")
args = parser.parse_args()
if args.command == "module":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.module.packages import ModulePackages
    project_root = Path(__file__).resolve().parents[1]
    trust_root = Path(os.environ.get("VERTEP_MODULE_TRUST_ROOT", str(project_root / "config/module-trust")))
    keys = {path.stem: path.read_bytes() for path in trust_root.glob("*.pem")}
    packages = ModulePackages(Path(os.environ.get("VERTEP_MODULE_ROOT", str(project_root / "data/modules"))), keys)
    if args.operation != "list" and not args.target:
        parser.error("module operation requires a target")
    if args.operation == "install":
        result = packages.install(json.loads(Path(args.target).read_text(encoding="utf-8")))
    elif args.operation == "uninstall":
        packages.uninstall(args.target)
        result = {"uninstalled": args.target}
    elif args.operation == "info":
        result = packages.info(args.target)
    else:
        result = packages.list()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0)
paths = {"jobs":"/api/jobs", "workers":"/api/workers", "characters":"/api/characters",
         "workflows":"/api/workflows", "metrics":"/api/metrics"}
method, body = "GET", None
if args.command == "create":
    path, method, body = "/api/jobs", "POST", {"topic": args.topic, "character_id": args.character}
elif args.command == "action":
    path, method = f"/api/jobs/{args.job_id}/{args.action}", "POST"
else:
    path = paths[args.command]
headers = {"Content-Type": "application/json"}
if os.getenv("ADMIN_PASSWORD"):
    credentials = f"{os.getenv('ADMIN_USER','admin')}:{os.environ['ADMIN_PASSWORD']}"
    headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
request = urllib.request.Request(args.url.rstrip("/") + path, data=json.dumps(body).encode() if body else None,
                                 headers=headers, method=method)
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
