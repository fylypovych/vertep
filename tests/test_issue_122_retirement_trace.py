"""Issue #122 P12 — the retirement trace may not write other Issues off silently.

§8 allows a point of the old list to be retired only with the complete chain

    criterion → replacement/owning issue → parity evidence → owner's decision

and forbids writing other Issues off automatically. These tests drive
``scripts/issue-122-retirement-trace.py`` with synthetic issue bodies: no network, no
mutating GitHub call, and no decision taken on the owner's behalf.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "issue-122-retirement-trace.py"
REGISTRY_HEADING = "## Повний реєстр i / ir / var"


@pytest.fixture(scope="module")
def trace_module():
    spec = importlib.util.spec_from_file_location("issue_122_retirement_trace", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body_with(rows: str, *, heading: str = REGISTRY_HEADING) -> str:
    return (
        "# Issue #122 — план\n"
        "## A\n"
        f"{heading}\n"
        "| GitHub Issue | Чинний ідентифікатор | Status | Перекриття | Рішення / залишок |\n"
        "|---|---|---|---|---|\n"
        f"{rows}"
        "## B\n"
        "Trailing text.\n"
    )


KEPT = ("| #61 | `i.0.0.2.5` | open | Voice audio | лишається: реалізація в #122 P3 |\n")
RETIRED_WITH_CHAIN = (
    "| #47 | `i.0.0.1.2` | open | Music bed | вилучити на користь #80; parity §9.2 "
    "та tests/test_music_bed.py |\n"
)
RETIRED_WITHOUT_OWNER = "| #47 | `i.0.0.1.2` | open | Music bed | вилучити, старе більше не потрібне |\n"
RETIRED_WITHOUT_EVIDENCE = "| #47 | `i.0.0.1.2` | open | Music bed | вилучити на користь #80 |\n"
NO_DECISION = "| #47 | `i.0.0.1.2` | open | Music bed |  |\n"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_the_registry_rows_are_read_from_the_canonical_table(trace_module):
    rows = trace_module.parse_registry(body_with(KEPT + RETIRED_WITH_CHAIN))

    assert [row["issue"] for row in rows] == ["#61", "#47"]
    assert rows[0]["identifier"] == "`i.0.0.2.5`"
    assert rows[0]["status"] == "open"
    assert rows[0]["overlap"] == "Voice audio"


def test_a_kept_row_is_not_a_retirement(trace_module):
    verdict = trace_module.classify(trace_module.parse_registry(body_with(KEPT))[0])

    assert verdict["verdict"] == "kept"
    assert verdict["problems"] == []


def test_a_negated_mention_of_retirement_is_not_a_retirement(trace_module):
    rows = trace_module.parse_registry(body_with(
        "| #47 | `i.0.0.1.2` | open | Music bed | не вилучати; лише за рішенням власника |\n"))

    assert trace_module.classify(rows[0])["verdict"] == "kept"


def test_the_table_must_exist(trace_module):
    with pytest.raises(ValueError, match="no .* section"):
        trace_module.parse_registry("# Issue #122\n## A\nNo registry here.\n")


def test_an_empty_registry_is_refused(trace_module):
    with pytest.raises(ValueError, match="empty"):
        trace_module.parse_registry(body_with(""))


def test_the_table_ends_at_the_next_section(trace_module):
    rows = trace_module.parse_registry(body_with(KEPT))

    assert len(rows) == 1
    assert rows[0]["issue"] == "#61"


# ---------------------------------------------------------------------------
# The chain: criterion → owner → evidence → owner decision
# ---------------------------------------------------------------------------


def test_a_retirement_with_owner_and_evidence_is_accepted(trace_module):
    verdict = trace_module.classify(trace_module.parse_registry(body_with(RETIRED_WITH_CHAIN))[0])

    assert verdict["retires"] is True
    assert verdict["verdict"] == "retire_with_evidence"
    assert verdict["problems"] == []


def test_a_retirement_without_a_replacement_owner_is_blocked(trace_module):
    verdict = trace_module.classify(trace_module.parse_registry(body_with(RETIRED_WITHOUT_OWNER))[0])

    assert verdict["verdict"] == "blocked"
    assert any("owning issue" in problem for problem in verdict["problems"])


def test_a_retirement_without_parity_evidence_is_blocked(trace_module):
    verdict = trace_module.classify(trace_module.parse_registry(body_with(RETIRED_WITHOUT_EVIDENCE))[0])

    assert verdict["verdict"] == "blocked"
    assert any("parity evidence" in problem for problem in verdict["problems"])


def test_a_row_without_a_decision_is_blocked(trace_module):
    verdict = trace_module.classify(trace_module.parse_registry(body_with(NO_DECISION))[0])

    assert verdict["verdict"] == "blocked"
    assert verdict["problems"] == ["no decision recorded"]


# ---------------------------------------------------------------------------
# The trace artefact
# ---------------------------------------------------------------------------


def test_zero_candidates_is_not_an_approval(trace_module):
    trace = trace_module.build_trace(body_with(KEPT))
    decision = trace["owner_decision"]

    assert trace["summary"] == {"rows": 1, "kept": 1, "retire_with_evidence": 0,
                                "blocked": 0, "duplicate_issues": []}
    assert decision.startswith("required")
    assert "не робимо old" in decision
    assert "not an approval" in decision


def test_a_duplicate_issue_number_is_reported(trace_module):
    trace = trace_module.build_trace(body_with(KEPT + KEPT))

    assert trace["summary"]["duplicate_issues"] == ["#61"]


def test_the_markdown_trace_names_every_row_and_its_problem(trace_module):
    rendered = trace_module.render_markdown(trace_module.build_trace(
        body_with(KEPT + RETIRED_WITHOUT_OWNER)))

    assert "| #61 |" in rendered and "| #47 |" in rendered
    assert "owning issue" in rendered
    assert "kept" in rendered and "blocked" in rendered


def test_a_clean_registry_passes_and_a_blocked_one_fails(trace_module, tmp_path, capsys):
    ok = trace_module.main([
        "--body", str(_write(tmp_path / "clean.md", body_with(KEPT))),
        "--out", str(tmp_path / "clean"),
    ])
    assert ok == 0

    blocked = trace_module.main([
        "--body", str(_write(tmp_path / "blocked.md", body_with(RETIRED_WITHOUT_OWNER))),
        "--out", str(tmp_path / "blocked"),
    ])
    assert blocked == 1
    assert "owning issue" in capsys.readouterr().out


def test_a_duplicate_registry_fails(trace_module, tmp_path):
    assert trace_module.main([
        "--body", str(_write(tmp_path / "dup.md", body_with(KEPT + KEPT))),
        "--out", str(tmp_path / "dup"),
    ]) == 1


def test_a_body_without_a_registry_is_refused_without_a_trace(trace_module, tmp_path, capsys):
    code = trace_module.main([
        "--body", str(_write(tmp_path / "none.md", "# Issue #122\n")),
        "--out", str(tmp_path / "none"),
    ])

    assert code == 2
    assert "cannot trace" in capsys.readouterr().err


def test_the_trace_is_written_as_json_and_markdown(trace_module, tmp_path):
    out = tmp_path / "out"
    trace_module.main([
        "--body", str(_write(tmp_path / "body.md", body_with(KEPT + RETIRED_WITH_CHAIN))),
        "--out", str(out),
    ])

    trace = json.loads((out / "issue-122-p12-retirement.json").read_text(encoding="utf-8"))
    assert trace["issue"] == "fylypovych/vertep#122"
    assert trace["summary"]["retire_with_evidence"] == 1
    assert (out / "issue-122-p12-retirement.md").read_text(encoding="utf-8").startswith(
        "# Vertep #122 P12")


def test_the_trace_only_reads_the_issue_and_never_writes_to_it(trace_module):
    """The trace may read Issue #122; every gh call must be a read-only subcommand."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]

    assert calls, "the trace must reach the issue through a subprocess call"
    mutating = {"comment", "close", "edit", "reopen", "create", "delete"}
    for call in calls:
        tokens = [
            element.value for element in call.args[0].elts
            if isinstance(element, ast.Constant)
        ] if call.args and isinstance(call.args[0], ast.List) else []
        assert tokens[:3] == ["gh", "issue", "view"], f"unexpected command {tokens}"
        assert not mutating.intersection(tokens), f"the trace would mutate the issue: {tokens}"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path