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
from starlette.concurrency import run_in_threadpool

REPORT_KINDS = {"audit", "review", "report"}
MAX_CONTEXT_BYTES = 20000


MAX_LISTED_FILES = 500
NOT_ACCEPTANCE = "Звіт моделі не є доказом проходження критеріїв приймання."


def _collect_context(root: str, paths: list) -> tuple[str, dict]:
    """Workspace context for the model plus an explicit account of its limits.

    Only files named in ``paths`` and contained in the workspace are read; the
    file listing alone is never treated as examined code.  The coverage record
    states what was actually read, what was missing, rejected or truncated, so
    a report cannot pass for a verified implementation (Issue #467).
    """
    requested = [str(name) for name in (paths or [])]
    coverage: dict = {"workspace_available": False, "listed_files": 0, "listing_truncated": False,
                      "requested_paths": requested, "read": [], "missing": [],
                      "outside_workspace": [], "not_read_limit": [],
                      "context_limit_chars": MAX_CONTEXT_BYTES, "code_examined": False}
    base = Path(root or "")
    if not root or not base.is_dir():
        coverage["limitations"] = _limitations(coverage)
        return "(робоча копія недоступна)", coverage
    coverage["workspace_available"] = True
    # The control image has no git, so the listing walks the tree.
    listing = sorted(str(path.relative_to(base)) for path in base.rglob("*")
                     if path.is_file() and ".git" not in path.relative_to(base).parts)
    coverage["listed_files"] = len(listing)
    coverage["listing_truncated"] = len(listing) > MAX_LISTED_FILES
    parts = ["Файли (лише назви, вміст не прочитано):\n" + "\n".join(listing[:MAX_LISTED_FILES])]
    remaining = MAX_CONTEXT_BYTES
    resolved = base.resolve()
    for name in requested:
        target = (base / name).resolve()
        if not target.is_relative_to(resolved):
            coverage["outside_workspace"].append(name)
            continue
        if not target.is_file():
            coverage["missing"].append(name)
            continue
        if remaining <= 0:
            coverage["not_read_limit"].append(name)
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            coverage["missing"].append(name)
            continue
        text = content[:remaining]
        remaining -= len(text)
        coverage["read"].append({"path": name, "chars": len(text), "total_chars": len(content),
                                 "truncated": len(text) < len(content)})
        parts.append(f"--- {name}\n{text}")
    coverage["code_examined"] = bool(coverage["read"])
    coverage["limitations"] = _limitations(coverage)
    return "\n\n".join(parts), coverage


def _limitations(coverage: dict) -> list[str]:
    notes = []
    if not coverage["workspace_available"]:
        notes.append("Робоча копія недоступна: вміст коду не досліджено.")
    elif not coverage["requested_paths"]:
        notes.append("Не задано paths: модель отримала лише перелік файлів, вміст коду не читався.")
    if coverage["listing_truncated"]:
        notes.append(f"Перелік файлів обрізано до {MAX_LISTED_FILES} з {coverage['listed_files']}.")
    if coverage["missing"]:
        notes.append("Відсутні або недоступні файли: " + ", ".join(coverage["missing"]) + ".")
    if coverage["outside_workspace"]:
        notes.append("Шляхи поза робочою копією відхилено: " + ", ".join(coverage["outside_workspace"]) + ".")
    truncated = [item["path"] for item in coverage["read"] if item["truncated"]]
    if truncated:
        notes.append(f"Вміст обрізано лімітом {MAX_CONTEXT_BYTES} символів: " + ", ".join(truncated) + ".")
    if coverage["not_read_limit"]:
        notes.append("Не прочитано через вичерпаний ліміт контексту: " + ", ".join(coverage["not_read_limit"]) + ".")
    if coverage["workspace_available"] and not coverage["read"]:
        notes.append("Жоден файл не прочитано: звіт не підтверджує реалізацію.")
    notes.append(NOT_ACCEPTANCE)
    return notes


def _workspace_context(root: str, paths: list) -> str:
    return _collect_context(root, paths)[0]


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
        # Workspace reads and the model call block, so they run outside the event
        # loop: /health and other requests stay served during inference (#468).
        context, coverage = await run_in_threadpool(
            _collect_context, (payload.get("workspace") or {}).get("root", ""),
            details.get("paths") or [])
        read = ", ".join(item["path"] for item in coverage["read"]) or "жодного"
        prompt = (f"Ти виконуєш роль {payload.get('role')} у лабораторії Vertep. "
                  f"Завдання типу {task.get('kind')}: {json.dumps(details, ensure_ascii=False)}\n"
                  "Лише звіт: нічого не вигадуй, посилайся на файли.\n"
                  f"Прочитаний вміст файлів: {read}. Про файли, вміст яких не прочитано, "
                  "не стверджуй, що їх перевірено; явно вкажи межі перевірки.\n"
                  "Межі перевірки:\n- " + "\n- ".join(coverage["limitations"]) + "\n\n" + context)
        try:
            answer = await run_in_threadpool(
                _call_model, payload.get("model") or lease.get("model") or "", prompt,
                lease["lease_id"], payload.get("limits") or {})
        except (RuntimeError, OSError, ValueError, urllib.error.URLError) as error:
            return {"status": "failed", "error": str(error)}
        tokens = answer.get("eval_count")
        return {"status": "ok", "tokens_used": tokens if type(tokens) is int else None,
                "result": {"summary": str(answer.get("response") or ""),
                           "report_only": True, "code_examined": coverage["code_examined"],
                           "files_read": coverage["read"], "context": coverage,
                           "limitations": coverage["limitations"]}}

    return app
