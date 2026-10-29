"""Automatic `ir` acceptance and atomic transfer — real implementation calls.

Issue #250 (`i.0.0.2.50`, L5–L6) requires every scenario to go through the
actual `lab.governance` code: an `ir` closes only after complete, independent
evidence plus an owner grant; the §4.7 successor sequence (create → verify →
close) is one fail-closed transaction; a retry never duplicates an issue and
never loses criteria.
"""

from datetime import datetime, timedelta, timezone
import pytest
from tests.lab_evidence_fixture import signed_contract_evidence


@pytest.fixture(autouse=True)
def review_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.setenv("LAB_REVIEW_TRUST_ROOT", str(tmp_path / "trusted-reviewers"))

from lab.governance import Governance, IssueTransport, RemoteRejected
from lab.policy import PolicyGate, reserve_identifier


class _FakeTransport(IssueTransport):
    """Recording transport for tests that must not touch GitHub."""

    def __init__(self):
        super().__init__(dry_run=False)
        self.created = []
        self.closed = []
        self.duplicates = []
        self.calls = 0

    def create_issue(self, title, body, labels=()):
        self.calls += 1
        self.created.append({"title": title, "body": body, "labels": list(labels)})
        return {"number": 100 + len(getattr(self, "previous", [])) + len(self.created),
                "url": f"https://github.com/owner/repo/issues/{100 + len(getattr(self, 'previous', [])) + len(self.created)}",
                "dry_run": self.dry_run}

    def find_duplicates(self, title):
        self.calls += 1
        self.duplicates.append(title)
        return []

    def get_issue(self, number):
        return (getattr(self, "previous", []) + self.created)[int(number) - 101]

    def close_issue(self, issue_number, comment=""):
        self.calls += 1
        self.closed.append({"number": issue_number, "comment": comment})
        return {"number": issue_number, "state": "closed"}


class _FlakyCreateTransport(_FakeTransport):
    """Fails the given create call (crash between two successors)."""

    def __init__(self, fail_on_call: int):
        super().__init__()
        self.fail_on_call = fail_on_call
        self.attempts = 0

    def create_issue(self, title, body, labels=()):
        self.attempts += 1
        if self.attempts == self.fail_on_call:
            raise RemoteRejected("controlled remote rejection before creating the successor")
        return super().create_issue(title, body, labels)


def _gate(tmp_path) -> PolicyGate:
    return PolicyGate(tmp_path / "state", activated=True)


def _gov(tmp_path, gate, transport) -> Governance:
    return Governance(gate, transport, state_dir=tmp_path / "gov",
                      repository="owner/repo")


def _grant(gate, action: str, subject: str):
    expires = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    gate.register_grant(actor="lab", action=action, subject=subject,
                        approved_by="owner", expires_at=expires)


def _full_evidence() -> dict:
    return signed_contract_evidence({
        "identifier": "ir.0.0.0.1",
        "kind": "real_server_verification",
        "version": "0.0.2.68",
        "commit": "fab401e1",
        "issue_number": 41,
        "environment": "ubuntu-24.04 clean install, physical GPU node",
        "expected": "всі тести пройдено на реальному стенді",
        "actual": "pytest: 8 passed, 0 failed, 0 skipped",
        "reviewer": "qa-standalone",
        "test_results": {"passed": 8, "failed": 0, "skipped": 0},
    })


def _partial_evidence() -> dict:
    evidence = _full_evidence()
    evidence["test_results"] = {"passed": 5, "failed": 2, "skipped": 1}
    return evidence


def _source_ir() -> dict:
    return {
        "identifier": "ir.0.0.0.1",
        "kind": "real_server_verification",
        "title": "Перевірка GPU lease на реальному стенді",
        "number": 41,
        "remaining_criteria": ["4.3", "4.4"],
    }


def _successors() -> list:
    return [
        {"identifier": "ir.0.0.0.2", "title": "Перевірка lease: черги",
         "transferred_criteria": ["4.3"]},
        {"title": "Перевірка lease: відновлення",
         "transferred_criteria": ["4.4"]},
    ]


# -- L6: evidence before any close ------------------------------------
@pytest.mark.parametrize("field,value", [("commit", "fab401e1"), ("commit", "f" * 40),
    ("expected", "changed acceptance"), ("actual", "different result"),
    ("identifier", "ir.0.0.0.2"), ("reviewer", "untrusted-reviewer")])
def test_mismatched_source_result_or_review_never_closes(tmp_path, field, value):
    gate = _gate(tmp_path)
    _grant(gate, "delegate_close_ir", "ir.0.0.0.1")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)
    evidence = _full_evidence()
    evidence[field] = value
    assert not governance.delegate_close_ir("ir.0.0.0.1", evidence).get("closed", False)
    assert transport.closed == []


def test_ir_close_without_evidence_is_refused(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.delegate_close_ir("ir.0.0.0.1")
    assert not outcome["allowed"]
    assert outcome["rule"] == "incomplete_evidence"
    assert transport.closed == []


def test_unconfirmed_remote_close_never_becomes_durable_acceptance(tmp_path):
    gate = _gate(tmp_path)
    _grant(gate, "delegate_close_ir", "ir.0.0.0.1")
    class RefusedClose(_FakeTransport):
        def close_issue(self, issue_number, comment=""):
            return {"number": issue_number, "state": "open"}
    governance = _gov(tmp_path, gate, RefusedClose())
    assert not governance.delegate_close_ir("ir.0.0.0.1", _full_evidence())["closed"]
    assert governance.closed() == {}


def test_partial_test_results_are_rejected(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    verdict = governance.submit_ir_evidence("ir.0.0.0.1", _partial_evidence())
    assert not verdict["accepted"]
    assert verdict["problems"]

    outcome = governance.delegate_close_ir("ir.0.0.0.1", _partial_evidence())
    assert not outcome["allowed"]
    assert outcome["rule"] == "incomplete_evidence"
    assert transport.closed == []


def test_self_reviewed_evidence_is_rejected(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    for identity in ("lab", "author", "self"):
        evidence = _full_evidence()
        evidence["reviewer"] = identity
        verdict = governance.submit_ir_evidence("ir.0.0.0.1", evidence)
        assert not verdict["accepted"], identity

    evidence = _full_evidence()
    evidence["reviewer"] = governance.actor
    assert not governance.submit_ir_evidence("ir.0.0.0.1", evidence)["accepted"]


def test_close_without_grant_is_denied_even_with_full_evidence(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.delegate_close_ir("ir.0.0.0.1", _full_evidence())
    assert not outcome["allowed"]
    assert outcome["rule"] == "no_active_grant"
    assert transport.closed == []


def test_close_happens_only_after_grant_and_full_evidence(tmp_path):
    gate = _gate(tmp_path)
    _grant(gate, "delegate_close_ir", "ir.0.0.0.1")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.delegate_close_ir("ir.0.0.0.1", _full_evidence())
    assert outcome["allowed"] and outcome["closed"]
    assert len(transport.closed) == 1
    assert "qa-standalone" in transport.closed[0]["comment"]
    effects = [r for r in gate.trail.records()
               if r.get("action") == "delegate_close_ir:effect"
               and r.get("decision") == "CLOSED"]
    assert effects and effects[-1]["subject"] == "ir.0.0.0.1"


def test_close_retry_never_closes_twice(tmp_path):
    gate = _gate(tmp_path)
    _grant(gate, "delegate_close_ir", "ir.0.0.0.1")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    first = governance.delegate_close_ir("ir.0.0.0.1", _full_evidence())
    second = governance.delegate_close_ir("ir.0.0.0.1", _full_evidence())
    assert first["closed"] and second["closed"]
    assert second.get("already") is True
    assert len(transport.closed) == 1
    assert governance.closed().get("ir.0.0.0.1")


# -- L6: atomic §4.7 transfer ------------------------------------------
def test_atomic_ir_transfer_sequence(tmp_path):
    gate = _gate(tmp_path)
    for action in ("consolidate_ir", "delegate_close_ir"):
        _grant(gate, action, "ir.0.0.0.1")
    _grant(gate, "create_ir_issue", "ir.0.0.0.2")
    _grant(gate, "create_ir_issue", "ir.0.0.0.3")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.consolidate_ir(_source_ir(), _successors())
    assert outcome["allowed"] and outcome["closed"]
    assert outcome["created"] == ["ir.0.0.0.2", "ir.0.0.0.3"]

    # successors first — exactly two, with reserved and explicit numbers
    assert [c["title"] for c in transport.created] == [
        "ir.0.0.0.2 — Перевірка lease: черги", "ir.0.0.0.3 — Перевірка lease: відновлення"]
    # only the source is closed, and only after verification
    assert len(transport.closed) == 1
    assert transport.closed[0]["number"] == 41

    records = {r["identifier"]: r for r in governance.created()
               if r.get("identifier")}
    for identifier in ("ir.0.0.0.2", "ir.0.0.0.3"):
        metadata = records[identifier].get("metadata") or {}
        assert metadata.get("transferred_from") == "ir.0.0.0.1"
        assert metadata.get("transferred_criteria")
    assert set(records["ir.0.0.0.2"]["metadata"]["transferred_criteria"]) | \
        set(records["ir.0.0.0.3"]["metadata"]["transferred_criteria"]) == {"4.3", "4.4"}

    transfer = governance.transfers()["ir.0.0.0.1"]
    assert transfer["successors"] == ["ir.0.0.0.2", "ir.0.0.0.3"]
    effects = [r for r in gate.trail.records()
               if r.get("action") == "consolidate_ir:effect"]
    assert effects and effects[-1]["decision"] == "CONSOLIDATED"

    # a repeated consolidation is a no-op
    again = governance.consolidate_ir(_source_ir(), _successors())
    assert again.get("already") is True
    assert len(transport.closed) == 1


def test_consolidation_without_grant_creates_nothing(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.consolidate_ir(_source_ir(), _successors())
    assert not outcome["allowed"]
    assert outcome["rule"] == "no_active_grant"
    assert transport.created == []
    assert transport.closed == []
    assert governance.transfers() == {}


def test_partial_coverage_refused_and_source_stays_open(tmp_path):
    gate = _gate(tmp_path)
    for action in ("consolidate_ir", "delegate_close_ir"):
        _grant(gate, action, "ir.0.0.0.1")
    _grant(gate, "create_ir_issue", "ir.0.0.0.2")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.consolidate_ir(
        _source_ir(), [{"identifier": "ir.0.0.0.2", "title": "Лише частину",
                        "transferred_criteria": ["4.3"]}])
    assert not outcome["allowed"]
    assert outcome["rule"] == "coverage"
    assert transport.created == []
    assert transport.closed == []


def test_successor_outside_source_scope_refused(tmp_path):
    gate = _gate(tmp_path)
    for action in ("consolidate_ir", "delegate_close_ir"):
        _grant(gate, action, "ir.0.0.0.1")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.consolidate_ir(
        _source_ir(), [{"title": "Нова вимога", "transferred_criteria": ["4.9"]}])
    assert not outcome["allowed"]
    assert outcome["rule"] == "scope_check"
    assert transport.created == []
    assert transport.closed == []


def test_duplicate_successor_titles_refused(tmp_path):
    gate = _gate(tmp_path)
    for action in ("consolidate_ir", "delegate_close_ir"):
        _grant(gate, action, "ir.0.0.0.1")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.consolidate_ir(_source_ir(), [
        {"title": "Однакові", "transferred_criteria": ["4.3"]},
        {"title": "Однакові", "transferred_criteria": ["4.4"]},
    ])
    assert not outcome["allowed"]
    assert outcome["rule"] == "scope_check"
    assert transport.created == []


def test_crash_between_successors_leaves_source_open_then_retry_completes(tmp_path):
    gate = _gate(tmp_path)
    for action in ("consolidate_ir", "delegate_close_ir"):
        _grant(gate, action, "ir.0.0.0.1")
    _grant(gate, "create_ir_issue", "ir.0.0.0.2")
    _grant(gate, "create_ir_issue", "ir.0.0.0.3")

    flaky = _FlakyCreateTransport(fail_on_call=2)
    governance = _gov(tmp_path, gate, flaky)

    outcome = governance.consolidate_ir(_source_ir(), _successors())
    assert outcome["allowed"]
    assert not outcome["closed"]
    assert outcome["created"] == ["ir.0.0.0.2"]
    assert flaky.closed == []
    assert governance.transfers() == {}

    # retry after the crash resumes with the same reserved numbers
    stable = _FakeTransport()
    stable.previous = list(flaky.created)
    governance.transport = stable
    retry = governance.consolidate_ir(_source_ir(), _successors())
    assert retry["allowed"] and retry["closed"]
    assert retry["created"] == ["ir.0.0.0.2", "ir.0.0.0.3"]
    # only the successor missing after the crash is (re)created — exactly one
    assert len(stable.created) == 1
    assert len(flaky.created) + len(stable.created) == 2
    identifiers = [r["identifier"] for r in governance.created()
                   if r.get("kind") == "ir"]
    assert sorted(identifiers) == ["ir.0.0.0.2", "ir.0.0.0.3"]
    assert len(stable.closed) == 1


# -- L5: one approval → exactly one issue ------------------------------
def test_one_approval_creates_exactly_one_issue(tmp_path):
    gate = _gate(tmp_path)
    _grant(gate, "create_ir_issue", "Випадковий залишок")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    first = governance.create_ir_issue("Випадковий залишок")
    second = governance.create_ir_issue("Випадковий залишок")
    assert first["created"] and first["identifier"] == "ir.0.0.0.1"
    assert not second["created"] and second["reason"] == "already created"
    assert len(transport.created) == 1

    other = governance.create_ir_issue("Інша назва без погодження")
    assert not other["allowed"]
    assert other["rule"] == "no_active_grant"
    assert len(transport.created) == 1


def test_denied_creation_never_touches_transport(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.create_ir_issue("test ir")
    assert not outcome["allowed"]
    assert outcome["rule"] == "no_active_grant"
    assert transport.calls == 0
    assert governance.created() == []


def test_var_without_approval_leaves_no_trace(tmp_path):
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    outcome = governance.create_var_issue("Архітектурна пропозиція")
    assert not outcome["allowed"]
    assert outcome["rule"] == "no_active_grant"
    assert transport.calls == 0
    assert governance.created() == []


def test_ir_creation_without_grant_is_denied(tmp_path):
    """Historical contract: rule name stays `no_active_grant`."""
    gate = _gate(tmp_path)
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    result = governance.create_ir_issue("test ir")
    assert not result["allowed"]
    assert result["rule"] == "no_active_grant"


def test_ir_closing_without_grant_is_denied(tmp_path):
    gate = _gate(tmp_path)
    decision = gate.decide("delegate_close_ir", actor="lab", subject="ir.0.0.0.1")
    assert not decision.allowed
    assert decision.rule == "no_active_grant"


# -- §4.2 shared continuous identifier series ---------------------------
def test_reserve_identifier_walks_the_shared_series():
    assert reserve_identifier("ir", ["ir.0.0.0.1", "ir.0.0.0.99"]) == "ir.0.0.1.0"
    # one series for every kind: after var.0.0.1.8 comes i.0.0.1.9
    assert reserve_identifier("i", ["var.0.0.1.8", "lab.0.0.0.1"]) == "i.0.0.1.9"
    assert reserve_identifier("ir", []) == "ir.0.0.0.1"


def test_creation_reserves_a_number_held_by_another_kind(tmp_path):
    gate = _gate(tmp_path)
    _grant(gate, "create_ir_issue", "Наступний ir")
    transport = _FakeTransport()
    governance = _gov(tmp_path, gate, transport)

    # a pre-existing `var` issue owns the next number of the shared series
    (tmp_path / "gov").mkdir(exist_ok=True)
    from lab.state_store import write_json
    write_json(tmp_path / "gov" / "created-issues.json", [
        {"key": "var.0.0.0.7", "identifier": "var.0.0.0.7", "kind": "var",
         "title": "Попередній var"}])

    outcome = governance.create_ir_issue("Наступний ir")
    assert outcome["created"]
    assert outcome["identifier"] == "ir.0.0.0.8"
