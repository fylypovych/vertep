"""Static architecture gate: CORE must not perform direct generation.

Verifies that ``core/`` Python modules do not perform direct inference,
synthesis, AI render, or upload operations that should be dispatched to
capability Workers via the task queue.

Principle: **Vertep does not generate content — it orchestrates tools that
generate content.**  CORE only creates/orchestrates tasks and accepts results.

Allowed control-plane operations (FFmpeg assembly, config queries, status
reporting) and explicit LOCAL_WORKER_FALLBACK paths are allowlisted below.
Any new generation call in ``core/`` not in the allowlist fails the suite.

i.0.0.1.4 (#104): the gate parses real Python AST (not line regex), so
aliased imports, attribute aliases and indirect calls resolve to their true
target instead of slipping past a line pattern.

See also: AGENTS.md (i.0.0.0.29), docs/core-generation-gate.md
"""

import ast
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parent.parent / "core"

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
]

_PROVIDER_SLOTS = {
    "llm", "tts", "compute", "image", "video", "publisher",
    "video_engine", "assembly",
}


def _rel(p: Path) -> str:
    """Return a ``core/...``-style project-relative path for a file path."""
    text = str(p).replace("\\", "/")
    idx = text.find("core/")
    return text[idx:] if idx >= 0 else text


def _collect_core_files() -> list[Path]:
    """Collect all Python files under core/ (excluding __pycache__)."""
    return sorted(p for p in CORE_ROOT.rglob("*.py") if "__pycache__" not in str(p))


class _AliasResolver(ast.NodeVisitor):
    """Resolve names/aliases to dotted paths; record forbidden calls."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.aliases: dict[str, str] = {}
        self.violations: list[dict] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for name in node.names:
            local = name.asname or name.name
            self.aliases[local] = f"{module}.{name.name}" if module else name.name
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            local = (name.asname or name.name).split(".")[0]
            self.aliases[local] = name.name
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        value_path = self._expr_path(node.value)
        if value_path:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = value_path
        self.generic_visit(node)

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
            last = func_path.split(".")[-1]
            return last if last in _PROVIDER_SLOTS else func_path
        if isinstance(node, ast.Subscript):
            return self._expr_path(node.value)
        return None

    def visit_Call(self, node: ast.Call) -> None:
        func_path = self._expr_path(node.func)
        if func_path:
            for target, description, category in FORBIDDEN_TARGETS:
                if func_path == target or func_path.endswith(f".{target}"):
                    self.violations.append({
                        "file": self.rel, "lineno": node.lineno,
                        "line": f"{func_path}()",
                        "description": description, "category": category,
                    })
                    break
        self.generic_visit(node)


def _scan_file(filepath: Path) -> list[dict]:
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []
    resolver = _AliasResolver(_rel(filepath))
    resolver.visit(tree)
    return resolver.violations


@pytest.fixture(scope="module")
def core_violations():
    """Scan every core file AST for forbidden generation targets."""
    violations = []
    for filepath in _collect_core_files():
        violations.extend(_scan_file(filepath))
    return violations


# ---------------------------------------------------------------------------
# Allowlist of known, documented occurrences.
# Each entry: (core-relative path, the exact call site, reason).
# These are control-plane operations or explicit LOCAL_WORKER_FALLBACK paths.
#
# An exception is identified by the *call site itself*, not by a line number: an
# unrelated edit above the line would otherwise turn a documented fallback into a
# false positive, and the reason for an exception would drift away from the code it
# describes. Every entry must still match exactly one real hit — removing the call or
# moving it to another file fails the gate.
ALLOWLIST = {
    # LLM — ScriptAgent local fallback when no Text Worker is available
    # (guarded by local_fallback_allowed() / _has_text_worker()).
    ("core/api/job_helpers.py", "script_agent.ScriptAgent.generate_script"):
        "LOCAL_WORKER_FALLBACK: ScriptAgent().generate_script() when no Text Worker",

    # LLM — ScriptAgent is the shared LLM/script inference implementation. It is
    # *executed on the Text Worker* (worker/role_executor.py) and wrapped as the
    # provider LLM backend (adapters/providers/DefaultLLMProvider). It is not
    # CORE orchestration performing inference; this line is the definition site,
    # not a CORE dispatch-path invocation.
    ("core/script_agent.py", "adapters.llm_clients.get_llm_client"):
        "ScriptAgent = LLM inference impl executed on Text Worker / wrapped as LLMProvider",

    # PUBLISH — CORE-side single-channel publication used ONLY by the local
    # fallback (guarded by local_fallback_allowed()); the Worker path goes
    # through the task queue instead.
    ("core/pipeline.py", "publisher.publish"):
        "LOCAL_WORKER_FALLBACK: _do_publish_single() when no Publisher Worker",

    # VIDEO — native FFmpeg assembly in CORE is control-plane (AGENTS.md §20,
    # i.0.0.1.4 #104). It is reachable only after the _is_native_engine()
    # fail-closed guard; external engines are dispatched to a Worker above.
    ("core/pipeline.py", "video_engine.render"):
        "Control-plane: native FFmpeg assembly after _is_native_engine() guard (#104)",
}


def _allowed(violation) -> str | None:
    """Reason an allowlist entry covers this violation, or ``None``."""
    for (allowed_file, call_site), reason in ALLOWLIST.items():
        if violation["file"] == allowed_file and call_site in violation["line"]:
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
            "AGENTS.md §1: Vertep does not generate content — it orchestrates.",
            "Fix: dispatch these calls to Workers via the task queue.",
            "",
        ]
        for v in unauthorized:
            lines.append(f"  {v['file']}:{v['lineno']} [{v['category']}] {v['description']}")
            lines.append(f"    -> {v['line']}")
            lines.append("")
        pytest.fail("\n".join(lines))

    def test_every_generation_call_is_accounted_for(self, core_violations):
        """All found calls must be allowlisted or fail with a clear message."""
        unauthorized = self._unauthorized(core_violations)
        assert not unauthorized, (
            f"{len(unauthorized)} unauthorized generation call(s) in CORE."
        )

    def test_allowlist_entries_are_current(self, core_violations):
        """Each allowlist entry must still match exactly one real source hit."""
        stale = []
        for (allowed_file, call_site), reason in ALLOWLIST.items():
            hits = [v for v in core_violations
                    if v["file"] == allowed_file and call_site in v["line"]]
            if not hits:
                stale.append((allowed_file, call_site, reason))
            elif len(hits) > 1:
                stale.append((allowed_file, call_site,
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