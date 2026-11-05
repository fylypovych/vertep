"""Built-in role executor: read-only report tasks on the leased production GPU model.

Serves the ``role-executor/1`` contract of :mod:`lab.executor`. It reads the isolated
workspace and returns text; it never changes code, runs tests or publishes anything,
so any other task kind is answered ``blocked``. Without a lease there is no model call.
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

REPORT_KINDS = {"audit", "review", "report"}
MAX_CONTEXT_BYTES = 20000


def _workspace_context(root: str, paths: list) -> str:
    base = Path(root or "")
    if not root or not base.is_dir():
        return "(робоча копія недоступна)"
    # The control image has no git, so the listing walks the tree.
    listing = sorted(str(path.relative_to(base)) for path in base.rglob("*")
                     if path.is_file() and ".git" not in path.relative_to(base).parts)
    # ponytail: fixed byte budget, without paths only the listing; add retrieval if audits need more.
    parts = ["Файли:\n" + "\n".join(listing[:500])]
    remaining = MAX_CONTEXT_BYTES
    resolved = base.resolve()
    for name in paths:
        target = (base / str(name)).resolve()
        if not target.is_relative_to(resolved) or not target.is_file() or remaining <= 0:
            continue
        text = target.read_text(encoding="utf-8", errors="replace")[:remaining]
        remaining -= len(text)
        parts.append(f"--- {name}\n{text}")
    return "\n\n".join(parts)


def _call_model(model: str, prompt: str, lease_id: str, limits: dict) -> dict:
    url = os.getenv("LAB_RESOURCE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("LAB_RESOURCE_URL is not configured")
    body = {"model": model.removeprefix("ollama:"), "prompt": prompt}
    if limits.get("max_tokens"):
        body["options"] = {"num_predict": int(limits["max_tokens"])}
    request = urllib.request.Request(
        f"{url}/inference/api/generate", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Vertep-Contract-Version": "1",
                 "X-Vertep-Lab-Token": os.getenv("LAB_RESOURCE_TOKEN", ""),
                 "X-Vertep-Lab-Lease": lease_id}, method="POST")
    context = ssl.create_default_context(cafile=os.getenv("LAB_RESOURCE_CA_FILE", "").strip() or None)
    with urllib.request.urlopen(request, timeout=float(limits.get("timeout_seconds") or 600),
                                context=context if url.startswith("https") else None) as response:
        return json.loads(response.read().decode("utf-8"))


def create_app() -> FastAPI:
    app = FastAPI(title="Vertep Lab role executor")

    @app.get("/health")
    def health() -> dict:
        return {"status": "HEALTHY"}

    @app.post("/invoke")
    async def invoke(request: Request) -> dict:
        expected = os.getenv("LAB_ROLE_EXECUTOR_TOKEN", "")
        if not expected or not secrets.compare_digest(
                request.headers.get("x-lab-executor-token", ""), expected):
            raise HTTPException(401, "Invalid executor token")
        payload = await request.json()
        task = payload.get("task") or {}
        if task.get("kind") not in REPORT_KINDS:
            return {"status": "blocked", "reason": f"task kind {task.get('kind')!r} is not supported"}
        lease = payload.get("resource_lease") or {}
        if not lease.get("lease_id"):
            return {"status": "blocked", "reason": "no active GPU lease"}
        details = task.get("payload") or {}
        context = _workspace_context((payload.get("workspace") or {}).get("root", ""),
                                     details.get("paths") or [])
        prompt = (f"Ти виконуєш роль {payload.get('role')} у лабораторії Vertep. "
                  f"Завдання типу {task.get('kind')}: {json.dumps(details, ensure_ascii=False)}\n"
                  "Лише звіт: нічого не вигадуй, посилайся на файли.\n\n" + context)
        try:
            answer = _call_model(payload.get("model") or lease.get("model") or "", prompt,
                                 lease["lease_id"], payload.get("limits") or {})
        except (RuntimeError, OSError, ValueError, urllib.error.URLError) as error:
            return {"status": "failed", "error": str(error)}
        tokens = answer.get("eval_count")
        return {"status": "ok", "tokens_used": tokens if type(tokens) is int else None,
                "result": {"summary": str(answer.get("response") or "")}}

    return app
