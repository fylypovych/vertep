"""GitHub governance proofs for the autonomous laboratory (Issue #107).

Policy gate ("script" of §3.5) is the single authority for every privileged
action; the governance module below shows the laboratory may create **only new
``lab`` issues**, never write, push or close anything, and never duplicate on a
retry.  These tests use fakes, so they prove the policy without a GitHub token
and without touching production state.
"""

import pytest

from lab.governance import Governance, GovernanceError, IssueTransport, render_lab_body
from lab.policy import PolicyGate


def _grant_create_lab_issue(gate, *subjects: str) -> None:
    from datetime import datetime, timedelta, timezone
    expires = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    for subject in subjects:
        gate.register_grant(actor="lab", action="create_lab_issue", subject=subject,
                            approved_by="owner", expires_at=expires)


class _RecordingTransport(IssueTransport):
    def __init__(self):
        self.created = []
        self.duplicates = []
        self.calls = 0

    def create_issue(self, title, body, labels=()):
        self.calls += 1
        self.created.append({"title": title, "labels": list(labels)})
        return {"number": 77, "url": "https://github.com/owner/repo/issues/77",
                "dry_run": self.dry_run}

    def find_duplicates(self, title):
        self.duplicates.append(title)
        return []


@pytest.fixture()
def governance(tmp_path):
    gate = PolicyGate(tmp_path / "state", activated=True)
    _grant_create_lab_issue(gate, "lab.0.0.0.1", "lab.0.0.0.2", "lab.0.0.0.4")
    return Governance(gate, _RecordingTransport(),
                      state_dir=tmp_path / "gov", repository="owner/repo")


def test_only_lab_issues_can_be_created(governance):
    result = governance.propose_lab_issue(
        {"identifier": "lab.0.0.0.1", "kind": "defect", "title": "Нулевий екземпляр",
         "evidence": "помилка при старті", "reproduction": "запустити",
         "risk": "низький", "expected_result": "робота без помилок",
         "boundaries": "тільки це питання",
         "source": "github.com/fylypovych/vertep/issues/107",
         "approved_scope": "Issue #107"})
    assert result["created"] is True
    assert result["issue"]["number"] == 77
    assert len(governance.transport.created) == 1


def test_lab_issue_without_validation_is_not_created(governance):
    with pytest.raises(GovernanceError) as error:
        governance.propose_lab_issue({"title": "Ідея", "evidence": "", "reproduction": "",
                                      "risk": "", "expected_result": "", "boundaries": "",
                                      "source": "", "approved_scope": ""})
    assert "product ideas" in str(error.value)


def test_duplicate_is_refused_on_retry(governance):
    finding = {"identifier": "lab.0.0.0.2", "kind": "defect", "title": "Помилка парсингу",
               "evidence": "desc", "reproduction": "desc", "risk": "desc",
               "expected_result": "desc", "boundaries": "desc",
               "source": "src", "approved_scope": "scope"}
    first = governance.propose_lab_issue(finding)
    assert first["created"] is True
    second = governance.propose_lab_issue(finding, key="lab.0.0.0.2")
    assert second["created"] is False
    assert second["reason"] == "already created"
    assert second["issue"]["number"] == 77


def test_not_a_lab_issue_is_refused(governance, monkeypatch):
    monkeypatch.setattr("lab.governance.validate_lab_finding",
                        lambda f: {"valid": False, "problems": ["nope"]})
    with pytest.raises(GovernanceError):
        governance.propose_lab_issue(
            {"identifier": "lab.0.0.0.3", "kind": "idea", "title": "Ідея",
             "evidence": "d", "reproduction": "d", "risk": "d",
             "expected_result": "d", "boundaries": "d", "source": "s",
             "approved_scope": "s"})


def test_edit_issue_is_refused(governance):
    report = governance.edit_issue("lab.0.0.0.1", "new body")
    assert report["allowed"] is False
    assert report["rule"] == "forbidden_action"


def test_push_is_refused(governance):
    report = governance.push()
    assert report["allowed"] is False
    assert report["rule"] == "forbidden_action"


def test_render_lab_body_is_stabilized_and_contains_required_fields():
    body = render_lab_body({
        "kind": "regression", "title": "Загублений тег",
        "evidence": "desc", "reproduction": "desc", "risk": "desc",
        "expected_result": "desc", "boundaries": "desc",
        "source": "github.com/fylypovych/vertep/issues/107",
        "approved_scope": "var.0.0.1.7"})
    assert "`lab` — знахідка" in body
    assert "## Доказ" in body
    assert "## Відтворення" in body
    assert "## Ризик" in body
    assert "## Очікуваний результат" in body
    assert "## Межі" in body
    assert "## Джерело та погоджений scope" in body
    assert "не нова функція" in body


def test_governance_logs_every_decision(tmp_path):
    transport = _RecordingTransport()
    gate = PolicyGate(tmp_path / "state", activated=True)
    _grant_create_lab_issue(gate, "lab.0.0.0.4")
    gov = Governance(gate, transport,
                     state_dir=tmp_path / "gov", repository="owner/repo")
    gov.propose_lab_issue({"identifier": "lab.0.0.0.4", "kind": "defect",
                           "title": "X", "evidence": "d", "reproduction": "d",
                           "risk": "d", "expected_result": "d", "boundaries": "d",
                           "source": "s", "approved_scope": "s"})
    trail = gov.gate.trail.records()
    actions = [r["action"] for r in trail]
    assert actions[-1] == "create_lab_issue"
    assert trail[-1]["decision"] == "CREATED"
