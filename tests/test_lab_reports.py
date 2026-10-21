"""Combined configurable reports over the one laboratory state (Issue #251, U3)."""

import pytest

from lab.cli import build_context
from lab.reports import ReportBuilder, ReportError


class Args:
    def __init__(self, state_dir, workspace):
        self.state_dir = state_dir
        self.workspace = workspace
        self.gpu = False
        self.role_executor_url = None


@pytest.fixture()
def builder(tmp_path, monkeypatch):
    for name in ("LAB_ROLE_EXECUTOR_URL", "LAB_POLICY_GATE_ACTIVE"):
        monkeypatch.delenv(name, raising=False)
    state = tmp_path / "state"
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    context = build_context(Args(state, workspace))
    return context["reports"], context


def test_all_sections_by_default(builder):
    reports, _ = builder
    payload = reports.build()
    assert payload["sections"] == list(ReportBuilder.available())
    assert set(payload["report"]) == set(ReportBuilder.available())
    assert payload["report"]["policy"]["activated"] is False
    assert "reviewer" in payload["report"]["roles"]["roles"]
    assert payload["report"]["gpu"] == {"enabled": False}


def test_client_picks_the_sections(builder):
    reports, _ = builder
    payload = reports.build(["status", "budget"])
    assert payload["sections"] == ["status", "budget"]
    assert set(payload["report"]) == {"status", "budget"}
    assert "runs_last_day" in payload["report"]["budget"]


def test_unknown_section_is_refused(builder):
    reports, _ = builder
    with pytest.raises(ReportError, match="unknown section"):
        reports.build(["status", "secrets"])


def test_report_reflects_the_live_governance_state(builder):
    from datetime import datetime, timedelta, timezone
    reports, context = builder
    context["gate"].activated = True
    context["gate"].register_grant(
        actor="lab", action="create_lab_issue", subject="lab.0.0.0.1",
        approved_by="owner",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=8)).isoformat())
    finding = {"identifier": "lab.0.0.0.1", "kind": "defect", "title": "Дефект",
               "evidence": "desc", "reproduction": "desc", "risk": "desc",
               "expected_result": "desc", "boundaries": "desc",
               "source": "src", "approved_scope": "scope"}
    context["governance"].propose_lab_issue(finding)

    payload = reports.build(["issues", "evidence", "rights", "grants",
                             "transfers", "notifications", "audit"])
    assert payload["report"]["issues"]["created"][0]["finding"]["identifier"] == "lab.0.0.0.1"
    assert payload["report"]["evidence"] == {"evidence": []}
    assert payload["report"]["rights"] == {"requests": []}
    assert [grant["action"] for grant in payload["report"]["grants"]["grants"]] == \
        ["create_lab_issue"]
    assert payload["report"]["transfers"] == {"transfers": {}}
    assert payload["report"]["notifications"]["web"]["critical"] is True
    assert payload["report"]["audit"]["verification"]["valid"] is True
