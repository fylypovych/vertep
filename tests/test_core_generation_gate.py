"""Static architecture gate: CORE must not perform direct generation.

Verifies that ``core/`` Python modules do not perform direct inference,
synthesis, AI render, or upload operations that should be dispatched to
capability Workers via the task queue.

Principle: **Vertep does not generate content — it orchestrates tools that
generate content.**  CORE only creates/orchestrates tasks and accepts results.

Allowed control-plane operations (FFmpeg assembly, config queries, status
reporting) and explicit LOCAL_WORKER_FALLBACK paths are allowlisted below.
Any new generation call in ``core/`` not in the allowlist fails the suite.

See also: AGENTS.md (i.0.0.0.29), docs/core-generation-gate.md
"""

import re
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parent.parent / "core"

# Forbidden patterns: a tuple (compiled_regex, description, category) for each
# direct generation / execution call that must never appear in CORE outside the
# documented allowlist.
FORBIDDEN_PATTERNS = [
    (re.compile(r"providers\.llm\(\)\.generate_script"), "Direct LLM generation", "LLM"),
    (re.compile(r"get_llm_client\(\)"), "Direct LLM client access", "LLM"),
    (re.compile(r"ScriptAgent\(\)\.generate_script"), "Direct ScriptAgent call", "LLM"),
    (re.compile(r"providers\.tts\(\)\.synthesize"), "Direct TTS synthesis", "TTS"),
    (re.compile(r"providers\.compute\(\)\.generate_output"), "Direct GPU compute", "GPU"),
    (re.compile(r"providers\.image\(\)\.generate"), "Direct image generation", "IMAGE"),
    (re.compile(r"providers\.video\(\)\.generate"), "Direct video generation", "VIDEO"),
    (re.compile(r"providers\.publisher\(\)\.publish"), "Direct publisher execution", "PUBLISH"),
    (re.compile(r"ComfyUIAdapter\(\)"), "Direct ComfyUIAdapter usage", "ADAPTER"),
    (re.compile(r"TTSAdapter\(\)"), "Direct TTSAdapter usage", "ADAPTER"),
]


def _rel(p: Path) -> str:
    """Return a ``core/...``-style project-relative path for a file path."""
    text = str(p).replace("\\", "/")
    idx = text.find("core/")
    return text[idx:] if idx >= 0 else text


def _collect_core_files() -> list[Path]:
    """Collect all Python files under core/ (excluding __pycache__)."""
    return sorted(p for p in CORE_ROOT.rglob("*.py") if "__pycache__" not in str(p))


@pytest.fixture(scope="module")
def core_violations():
    """Scan every core file line for the forbidden generation patterns."""
    violations = []
    for filepath in _collect_core_files():
        try:
            source = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for pattern, description, category in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    violations.append({
                        "file": _rel(filepath), "lineno": lineno,
                        "line": line.strip(), "description": description,
                        "category": category,
                    })
    return violations


# ---------------------------------------------------------------------------
# Allowlist of known, documented occurrences.
# Each entry: (core-relative path, exact line number, reason).
# These are control-plane operations or explicit LOCAL_WORKER_FALLBACK paths.
ALLOWLIST = {
    # LLM — ScriptAgent local fallback when no Text Worker is available
    # (guarded by LOCAL_WORKER_FALLBACK / _has_text_worker()).
    ("core/api/job_helpers.py", 217):
        "LOCAL_WORKER_FALLBACK: ScriptAgent().generate_script() when no Text Worker",

    # LLM — ScriptAgent is the shared LLM/script inference implementation. It is
    # *executed on the Text Worker* (worker/role_executor.py) and wrapped as the
    # provider LLM backend (adapters/providers/DefaultLLMProvider). It is not
    # CORE orchestration performing inference; this line is the definition site,
    # not a CORE dispatch-path invocation.
    ("core/script_agent.py", 21):
        "ScriptAgent = LLM inference impl executed on Text Worker / wrapped as LLMProvider",
}


class TestCoreGenerationGate:
    """CORE must not perform direct generation / execution."""

    def _unauthorized(self, violations):
        return [
            v for v in violations
            if (v["file"], v["lineno"]) not in ALLOWLIST
        ]

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
        """Each allowlist entry must correspond to an actual source hit."""
        hits = {(v["file"], v["lineno"]) for v in core_violations}
        stale = [(f, ln, r) for (f, ln), r in ALLOWLIST.items() if (f, ln) not in hits]
        assert not stale, f"Stale allowlist entries (no matching source hit): {stale}"


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