import argparse
import base64
import json
import os
import urllib.request

parser = argparse.ArgumentParser(prog="vertep-cli")
parser.add_argument("--url", default=os.getenv("VERTEP_CORE_URL", "http://127.0.0.1:8080"))
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("jobs")
create = sub.add_parser("create"); create.add_argument("topic"); create.add_argument("--character", default="did_samogon")
action = sub.add_parser("action"); action.add_argument("job_id"); action.add_argument("action", choices=["pause","resume","retry","regenerate","cancel","approve","publish"])
sub.add_parser("workers"); sub.add_parser("characters"); sub.add_parser("workflows"); sub.add_parser("metrics")
args = parser.parse_args()
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
