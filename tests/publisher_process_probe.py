"""Disposable Publisher process: only external platform HTTP is controlled."""
import json
import sys
from pathlib import Path

import httpx
from publishers import LIVE_PUBLISHERS, FakeTransport
from worker.role_executor import execute_role_task


def main():
    payload = json.load(sys.stdin)
    responses = [httpx.Response(item["status"], json=item.get("json", {}), headers=item.get("headers"))
                 for item in payload["responses"]]
    if payload.get("disconnect_after_receive"):
        def lost_response():
            raise httpx.ReadError("controlled response lost after upload received")
        responses[-1] = lost_response
    transport = FakeTransport(responses)
    LIVE_PUBLISHERS[payload["task"]["channel"]].transport = transport
    try:
        result = {"artifacts": execute_role_task("publisher", payload["task"])}
    except RuntimeError as error:
        result = {"error": str(error)}
    Path(payload["output"]).write_text(json.dumps({**result,
        "http_requests": [{"method": m, "url": u} for m, u, _ in transport.requests]}))


if __name__ == "__main__":
    main()
