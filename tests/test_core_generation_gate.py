"""Static architecture gate: CORE must not perform direct generation.

Verifies that ``core/`` Python modules do not perform direct inference,
synthesis, AI render, or upload operations that should be dispatched to
capability Workers via the task queue.

Principle: **Vertep does not generate content — it orchestrates tools that
generate content.**  CORE only creates/orchestrates tasks and accepts results.

Allowed control-plane operations (assembly planning/dispatch, config queries, status
reporting) and explicit LOCAL_WORKER_FALLBACK paths are allowlisted below.
Any new generation call in ``core/`` not in the allowlist fails the suite.

i.0.0.1.4 (#104): the gate parses real Python AST (not line regex), so
aliased imports, attribute aliases and indirect calls resolve to their true
target instead of slipping past a line pattern.

i.0.0.4.71 (#471): the gate also resolves ``with ... as`` clients,
``importlib.import_module`` / ``__import__`` and constant ``getattr`` access,
recognises direct HTTP inference requests (``httpx``/``requests``) by their
inference endpoint, and binds every exception to its enclosing function.
This is a static heuristic with documented limits (docs/core-generation-gate.md
§3.1), not a universal proof that CORE never generates content.

See also: AGENTS.md (i.0.0.0.29), docs/core-generation-gate.md
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = ROOT / "core"

# Forbidden *resolved* targets: (dotted path, description, category).
# Aliases resolve first, so ``engine.render()`` with
# ``engine = providers.video_engine()`` matches ``video_engine.render``.
FORBIDDEN_TARGETS = [
    ("llm.generate_script", "Direct LLM generation", "LLM"),
    ("get_llm_client", "Direct LLM client access", "LLM"),
    ("ScriptAgent.generate_script", "Direct ScriptAgent call", "LLM"),
    ("tts.synthesize", "Direct TTS synthesis", "TTS"),
    ("compute.generate_output", "Direct GPU compute", "GPU"),
    ("image.generate", "Direct image generation", "IMAGE"),
    ("video.generate", "Direct video generation", "VIDEO"),
    ("video_engine.render", "Direct VideoEngine render in CORE", "VIDEO"),
    ("publisher.publish", "Direct publisher execution", "PUBLISH"),
    ("ComfyUIAdapter", "Direct ComfyUIAdapter usage", "ADAPTER"),
    ("TTSAdapter", "Direct TTSAdapter usage", "ADAPTER"),
    # i.0.0.4.70 (#470): voice preview/synthesis runs on a Voice Worker.
    ("role_executor.synthesize_voice", "Direct TTS runtime synthesis in CORE", "TTS"),
    # i.0.0.4.71 (#471): the Worker role executors run generation themselves.
    ("role_executor.execute_role_task", "Worker role executor called in CORE", "WORKER"),
    ("role_executor.execute_text", "Worker text executor called in CORE", "WORKER"),
    ("role_executor.execute_script", "Worker script executor called in CORE", "WORKER"),
    ("role_executor.execute_storyboard", "Worker storyboard executor called in CORE", "WORKER"),
    ("role_executor.execute_voice", "Worker voice executor called in CORE", "WORKER"),
    ("role_executor.execute_image", "Worker image executor called in CORE", "WORKER"),
    ("role_executor.execute_video", "Worker video executor called in CORE", "WORKER"),
    ("role_executor.execute_publisher", "Worker publisher executor called in CORE", "WORKER"),
]

_PROVIDER_SLOTS = {
    "llm", "tts", "compute", "image", "video", "publisher",
    "video_engine", "assembly",
}

# i.0.0.4.71: HTTP clients whose requests are inspected for inference endpoints.
_HTTP_ROOTS = {"httpx", "requests"}
_HTTP_VERBS = {"get", "post", "put", "patch", "request", "stream", "send"}
# A request is inference when a *constant* part of its URL names the endpoint
# that runs a model: Ollama, OpenAI-compatible, the Vertep TTS runtime, ComfyUI.
# Config/status/probe endpoints (/api/tags, /api/pull, /models, /voices,
# /system_stats, /health) are deliberately absent.
INFERENCE_ENDPOINT = re.compile(
    r"(?:^|/)(api/generate|api/chat|api/embed(?:dings)?|chat/completions|v1/completions"
    r"|audio/speech|images/generations|synthesize|prompt)(?:$|[/?#])")
# Modules whose attributes run generation; dynamic access with a name that
# cannot be resolved statically is reported instead of silently trusted.
GENERATION_MODULES = {"worker.role_executor", "adapters.comfyui", "adapters.tts",
                      "adapters.llm_clients", "core.gpu.backends"}
_IMPORTERS = {"importlib.import_module", "import_module", "__import__"}


def _rel(p: Path) -> str:
    """Return a ``core/...``-style project-relative path for a file path."""
    text = str(p).replace("\\", "/")
    idx = text.find("core/")
    return text[idx:] if idx >= 0 else text


def _collect_core_files() -> list[Path]:
    """Collect all Python files under core/ (excluding __pycache__)."""
    return sorted(p for p in CORE_ROOT.rglob("*.py") if "__pycache__" not in str(p))


def _constant_text(node: ast.AST | None) -> str:
    """The statically known text of a URL expression; dynamic parts are '\\0'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_constant_text(value) if isinstance(value, ast.Constant) else "\0"
                       for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _constant_text(node.left) + _constant_text(node.right)
    return "\0"


class _AliasResolver(ast.NodeVisitor):
    """Resolve names/aliases to dotted paths; record forbidden calls."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.aliases: dict[str, str] = {}
        self.violations: list[dict] = []
        self.scope: list[str] = []

    # -- enclosing function -------------------------------------------------
    def _scoped(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _function(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _record(self, node: ast.AST, line: str, description: str, category: str) -> None:
        self.violations.append({
            "file": self.rel, "lineno": node.lineno, "function": self._function(),
            "line": line, "description": description, "category": category,
        })

    # -- aliases ------------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for name in node.names:
            local = name.asname or name.name
            self.aliases[local] = f"{module}.{name.name}" if module else name.name
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            if name.asname:
                self.aliases[name.asname] = name.name
            else:
                local = name.name.split(".")[0]
                self.aliases[local] = local
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        value_path = self._expr_path(node.value)
        if value_path:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = value_path
        self.generic_visit(node)

    def _with(self, node) -> None:
        for item in node.items:
            if isinstance(item.optional_vars, ast.Name):
                value_path = self._expr_path(item.context_expr)
                if value_path:
                    self.aliases[item.optional_vars.id] = value_path
        self.generic_visit(node)

    visit_With = visit_AsyncWith = _with

    def _expr_path(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._expr_path(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call):
            func_path = self._expr_path(node.func)
            if not func_path:
                return None
            first = node.args[0] if node.args else None
            if func_path in _IMPORTERS and isinstance(first, ast.Constant) \
                    and isinstance(first.value, str):
                return first.value
            if func_path == "getattr" and len(node.args) >= 2:
                base = self._expr_path(node.args[0])
                name = node.args[1]
                if base and isinstance(name, ast.Constant) and isinstance(name.value, str):
                    return f"{base}.{name.value}"
                return None
            last = func_path.split(".")[-1]
            return last if last in _PROVIDER_SLOTS else func_path
        if isinstance(node, ast.Subscript):
            return self._expr_path(node.value)
        return None

    # -- checks -------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func_path = self._expr_path(node.func)
        if func_path:
            for target, description, category in FORBIDDEN_TARGETS:
                if func_path == target or func_path.endswith(f".{target}"):
                    self._record(node, f"{func_path}()", description, category)
                    break
            else:
                self._check_http(node, func_path)
        self._check_dynamic(node, func_path)
        self.generic_visit(node)

    def _check_http(self, node: ast.Call, func_path: str) -> None:
        parts = func_path.split(".")
        if parts[0] not in _HTTP_ROOTS or parts[-1] not in _HTTP_VERBS:
            return
        index = 1 if parts[-1] in {"request", "stream"} else 0
        url = node.args[index] if len(node.args) > index else next(
            (item.value for item in node.keywords if item.arg == "url"), None)
        match = INFERENCE_ENDPOINT.search(_constant_text(url))
        if match:
            self._record(node, f"{func_path}(/{match.group(1)})",
                         "Direct HTTP inference request", "HTTP")

    def _check_dynamic(self, node: ast.Call, func_path: str | None) -> None:
        """``getattr(generation_module, <non-constant>)`` cannot be resolved."""
        if func_path != "getattr" or len(node.args) < 2:
            return
        if isinstance(node.args[1], ast.Constant):
            return
        base = self._expr_path(node.args[0])
        if base and any(base == module or base.endswith(f".{module}")
                        for module in GENERATION_MODULES):
            self._record(node, f"getattr({base}, <dynamic>)",
                         "Unresolvable dynamic access to a generation module", "DYNAMIC")


def _scan_source(source: str, rel: str) -> list[dict]:
    resolver = _AliasResolver(rel)
    resolver.visit(ast.parse(source, filename=rel))
    return resolver.violations


def _scan_file(filepath: Path) -> list[dict]:
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        return _scan_source(source, _rel(filepath))
    except SyntaxError:
        return []


@pytest.fixture(scope="module")
def core_violations():
    """Scan every core file AST for forbidden generation targets."""
    violations = []
    for filepath in _collect_core_files():
        violations.extend(_scan_file(filepath))
    return violations


# ---------------------------------------------------------------------------
# Allowlist of known, documented occurrences.
# Each entry: (core-relative path, enclosing function, call site) -> reason.
# These are control-plane operations, explicit LOCAL_WORKER_FALLBACK paths or a
# shared implementation that only a Worker executes.
#
# An exception is identified by the *call site itself inside its enclosing
# function*, not by a line number or the whole file: a new forbidden call in the
# same file but another function is not covered (i.0.0.4.71), and an unrelated
# edit above the line does not turn a documented exception into a false
# positive. Every entry must match exactly one real hit — removing the call,
# moving it to another function or adding a second one fails the gate.
ALLOWLIST = {
    # LLM — ScriptAgent local fallback when no Text Worker is available
    # (guarded by local_fallback_allowed() / _has_text_worker()).
    ("core/api/job_helpers/script.py", "_generate_script_local",
     "script_agent.ScriptAgent.generate_script"):
        "LOCAL_WORKER_FALLBACK: ScriptAgent().generate_script() when no Text Worker",

    # LLM — ScriptAgent is the shared LLM/script inference implementation. It is
    # *executed on the Text Worker* (worker/role_executor.py) and wrapped as the
    # provider LLM backend (adapters/providers/DefaultLLMProvider). It is not
    # CORE orchestration performing inference; this line is the definition site,
    # not a CORE dispatch-path invocation.
    ("core/script_agent.py", "ScriptAgent._complete", "adapters.llm_clients.get_llm_client"):
        "ScriptAgent = LLM inference impl executed on Text Worker / wrapped as LLMProvider",

    # PUBLISH — CORE-side single-channel publication used ONLY by the local
    # fallback (guarded by local_fallback_allowed()); the Worker path goes
    # through the task queue instead.
    ("core/pipeline.py", "_do_publish_single", "publisher.publish"):
        "LOCAL_WORKER_FALLBACK: _do_publish_single() when no Publisher Worker",

    # VIDEO — no CORE-side render remains: since i.0.0.4.98 (#498) every final
    # assembly, the native FFmpeg engine included, is dispatched to a Worker by
    # finalize_job(), so no allowlist entry exists for video_engine.render.

    # GPU routing backends — shared implementation imported only by the Worker
    # (worker/gpu_routing.py); TestSharedImplementationBoundary proves no CORE
    # runtime module imports it (i.0.0.4.71).
    ("core/gpu/backends.py", "OllamaProvider._infer", "httpx.post(/api/generate)"):
        "Shared impl executed by the Worker GPU router, never imported by CORE",
    ("core/gpu/backends.py", "OpenAIProvider._infer", "httpx.post(/chat/completions)"):
        "Shared impl executed by the Worker GPU router, never imported by CORE",

    # LAB — Ollama unload (keep_alive=0) before production takes the GPU back:
    # it frees VRAM and generates nothing (docs/lab/lab-gpu-contract.md).
    ("core/api/lab_lease.py", "_unload_model", "httpx.AsyncClient.post(/api/generate)"):
        "Control-plane: Ollama model unload with keep_alive=0, no inference",
}

# Modules that hold a Worker-executed shared implementation: CORE runtime code
# must not import them, otherwise their allowlisted calls would run in CORE.
WORKER_ONLY_MODULES = {"core/gpu/backends.py": "core.gpu.backends"}


def _allowed(violation) -> str | None:
    """Reason an allowlist entry covers this violation, or ``None``."""
    for (allowed_file, function, call_site), reason in ALLOWLIST.items():
        if (violation["file"] == allowed_file and violation["function"] == function
                and call_site in violation["line"]):
            return reason
    return None


class TestCoreGenerationGate:
    """CORE must not perform direct generation / execution."""

    def _unauthorized(self, violations):
        return [v for v in violations if _allowed(v) is None]

    def test_no_unauthorized_generation_calls(self, core_violations):
        unauthorized = self._unauthorized(core_violations)
        if not unauthorized:
            return

        lines = [
            "Direct generation calls detected in CORE that are NOT allowlisted.",
            "AGENTS.md §3.1: Vertep does not generate content — it orchestrates.",
            "Fix: dispatch these calls to Workers via the task queue.",
            "",
        ]
        for v in unauthorized:
            lines.append(f"  {v['file']}:{v['lineno']} in {v['function']} "
                         f"[{v['category']}] {v['description']}")
            lines.append(f"    -> {v['line']}")
            lines.append("")
        pytest.fail("\n".join(lines))

    def test_allowlist_entries_are_current(self, core_violations):
        """Each allowlist entry must still match exactly one real source hit."""
        stale = []
        for (allowed_file, function, call_site), reason in ALLOWLIST.items():
            hits = [v for v in core_violations
                    if v["file"] == allowed_file and v["function"] == function
                    and call_site in v["line"]]
            if not hits:
                stale.append((allowed_file, function, call_site, reason))
            elif len(hits) > 1:
                stale.append((allowed_file, function, call_site,
                              f"ambiguous: {len(hits)} matching call sites"))
        assert not stale, f"Stale or ambiguous allowlist entries: {stale}"

class TestAliasResolution:
    """i.0.0.1.4 (#104): the AST gate must catch alias/indirect calls."""

    def _scan(self, source: str) -> list[dict]:
        import ast as _ast
        from test_core_generation_gate import _AliasResolver
        tree = _ast.parse(source)
        resolver = _AliasResolver("core/sample.py")
        resolver.visit(tree)
        return resolver.violations

    def test_direct_call_detected(self):
        hits = self._scan("providers.video_engine().render(out)\n")
        assert any(h["category"] == "VIDEO" for h in hits)

    def test_attribute_alias_detected(self):
        hits = self._scan(
            "engine = providers.video_engine()\n"
            "engine.render(out)\n"
        )
        assert any("video_engine.render" in h["line"] for h in hits), hits

    def test_aliased_import_detected(self):
        hits = self._scan(
            "from core.script_agent import ScriptAgent as Agent\n"
            "Agent().generate_script(topic)\n"
        )
        assert hits, "aliased ScriptAgent call must not slip past the gate"

    def test_benign_dispatch_not_flagged(self):
        hits = self._scan(
            "task = _assembly_task_for(job)\n"
            "_enqueue_assembly_task(job, task)\n"
        )
        assert hits == []


class TestCoreGenerationGateDocumentation:
    """Verify architecture gate documentation exists and is current."""

    def test_gate_documentation_exists(self):
        doc = Path(__file__).resolve().parent.parent / "docs" / "core-generation-gate.md"
        assert doc.exists(), "Missing docs/core-generation-gate.md (i.0.0.0.29)"

    def test_agents_md_mentions_generation_gate(self):
        agents = Path(__file__).resolve().parent.parent / "AGENTS.md"
        assert "generation gate" in agents.read_text(encoding="utf-8").lower(), (
            "AGENTS.md must document the CORE generation gate (i.0.0.0.29)"
        )

class TestBlindSpots:
    """i.0.0.4.71 (#471): negative fixtures for the confirmed blind spots."""

    @staticmethod
    def _hits(source: str, rel: str = "core/sample.py") -> list[dict]:
        return _scan_source(source, rel)

    # -- synthesize_voice (A1) ----------------------------------------------
    def test_direct_synthesize_voice(self):
        hits = self._hits("from worker.role_executor import synthesize_voice\n"
                          "def preview():\n    return synthesize_voice('hi')\n")
        assert [(h["category"], h["function"]) for h in hits] == [("TTS", "preview")]

    def test_aliased_synthesize_voice(self):
        hits = self._hits("from worker.role_executor import synthesize_voice as say\nsay('hi')\n")
        assert [h["category"] for h in hits] == ["TTS"]

    def test_module_alias_synthesize_voice(self):
        hits = self._hits("import worker.role_executor as rx\nrx.synthesize_voice('hi')\n")
        assert [h["category"] for h in hits] == ["TTS"]

    def test_worker_executor_in_core(self):
        hits = self._hits("from worker.role_executor import execute_role_task\n"
                          "execute_role_task('voice', task)\n")
        assert [h["category"] for h in hits] == ["WORKER"]

    # -- dynamic import / getattr ---------------------------------------------
    @pytest.mark.parametrize("source", [
        "import importlib\nm = importlib.import_module('worker.role_executor')\nm.synthesize_voice('x')\n",
        "from importlib import import_module\nimport_module('worker.role_executor').synthesize_voice('x')\n",
        "m = __import__('worker.role_executor', fromlist=['x'])\nm.synthesize_voice('x')\n",
        "import worker.role_executor as rx\ngetattr(rx, 'synthesize_voice')('x')\n",
        "import importlib\nf = getattr(importlib.import_module('worker.role_executor'), 'synthesize_voice')\nf('x')\n",
    ])
    def test_dynamic_import_and_getattr(self, source):
        assert [h["category"] for h in self._hits(source)] == ["TTS"]

    def test_unresolvable_getattr_on_a_generation_module(self):
        hits = self._hits("import worker.role_executor as rx\ngetattr(rx, name)('x')\n")
        assert [h["category"] for h in hits] == ["DYNAMIC"]

    # -- direct HTTP inference ------------------------------------------------
    @pytest.mark.parametrize("source,endpoint", [
        ("import httpx\nhttpx.post(base + '/api/generate', json=b)\n", "api/generate"),
        ("import httpx\nhttpx.post(f'{base}/api/chat', json=b)\n", "api/chat"),
        ("import requests\nrequests.post(url=f'{base}/v1/chat/completions')\n", "chat/completions"),
        ("import httpx\nwith httpx.Client() as c:\n    c.post(f'{tts}/synthesize', json=b)\n", "synthesize"),
        ("import httpx\nasync def f():\n    async with httpx.AsyncClient() as c:\n"
         "        await c.post(f'{comfy}/prompt', json=b)\n", "prompt"),
        ("import httpx\nwith httpx.Client() as c:\n    c.stream('POST', base + '/api/generate')\n",
         "api/generate"),
        ("from httpx import post as send\nsend('http://ollama:11434/api/generate')\n", "api/generate"),
    ])
    def test_direct_http_inference(self, source, endpoint):
        hits = self._hits(source)
        assert [h["category"] for h in hits] == ["HTTP"], hits
        assert endpoint in hits[0]["line"]

    # -- control-plane must not become false positives ------------------------
    @pytest.mark.parametrize("source", [
        "import httpx\nhttpx.get(base + '/api/tags')\n",
        "import httpx\nwith httpx.Client() as c:\n    c.stream('POST', f'{base}/api/pull', json=b)\n",
        "import httpx\nhttpx.get(f'{tts}/voices')\n",
        "import httpx\nhttpx.get(f'{comfy}/system_stats')\n",
        "import httpx\nhttpx.get(f'{api}/models', headers=h)\n",
        "import httpx\nhttpx.get(f'{base}/health')\n",
        "import httpx\nhttpx.post(f'{core}/api/tasks/result', json=r)\n",
        "import httpx\nhttpx.get(f'{base}/api/prompts')\n",
        "from worker.role_executor import voice_catalog, list_text_models\nvoice_catalog()\nlist_text_models()\n",
        "task_queue.enqueue(build_task('hi', 'uk', 150))\n",
        "providers.publisher().available_channels()\nprovider_matrix()\n",
        "router.post('/api/models/voices/synthesize')\n",
    ])
    def test_control_plane_is_not_flagged(self, source):
        assert self._hits(source) == []

    # -- exceptions are bound to the enclosing function ------------------------
    def test_same_file_other_function_does_not_inherit_the_allowlist(self):
        source = ("from .script_agent import ScriptAgent\n"
                  "def _generate_script_local():\n    ScriptAgent().generate_script(t)\n"
                  "def new_shortcut():\n    ScriptAgent().generate_script(t)\n")
        hits = self._hits(source, "core/api/job_helpers/script.py")
        assert [h["function"] for h in hits] == ["_generate_script_local", "new_shortcut"]
        assert [_allowed(h) is not None for h in hits] == [True, False]

    def test_other_target_in_an_allowlisted_function_is_not_covered(self):
        source = ("import httpx\nasync def _unload_model(lease):\n"
                  "    async with httpx.AsyncClient() as c:\n"
                  "        await c.post(f'{e}/api/generate', json={})\n"
                  "        await c.post(f'{e}/api/chat', json={})\n")
        hits = self._hits(source, "core/api/lab_lease.py")
        assert [_allowed(h) is not None for h in hits] == [True, False]

    def test_method_scope_is_qualified(self):
        source = ("import httpx\nclass OllamaProvider:\n    def _infer(self, t):\n"
                  "        httpx.post(self.e + '/api/generate')\n"
                  "    def warmup(self):\n        httpx.post(self.e + '/api/generate')\n")
        hits = self._hits(source, "core/gpu/backends.py")
        assert [h["function"] for h in hits] == ["OllamaProvider._infer", "OllamaProvider.warmup"]
        assert [_allowed(h) is not None for h in hits] == [True, False]


class TestSharedImplementationBoundary:
    """An allowlisted Worker-only implementation must not be imported by CORE runtime."""

    @staticmethod
    def _imports(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = package.split(".")[:len(package.split(".")) - node.level + 1]
                    base = ".".join([*parent, base] if base else parent)
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.args[0] if node.args else None, ast.Constant):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name in {"import_module", "__import__"}:
                    found.add(str(node.args[0].value))
        return found

    @pytest.mark.parametrize("rel,module", sorted(WORKER_ONLY_MODULES.items()))
    def test_only_the_worker_imports_it(self, rel, module):
        importers = []
        for root in ("core", "services", "adapters", "scripts", "installer"):
            for path in sorted((ROOT / root).rglob("*.py")):
                if "__pycache__" in path.parts or path == ROOT / rel:
                    continue
                if module in self._imports(path):
                    importers.append(str(path.relative_to(ROOT)))
        assert importers == [], f"{module} must stay Worker-only; imported by {importers}"
        assert module in self._imports(ROOT / "worker" / "gpu_routing.py")
