"""Issue #467 (i.0.0.4.67): the LAB report executor states its code-context limits.

A report built from a file listing, a missing workspace, missing files or a
truncated context must say so; the files whose content was read are listed
separately from the names-only listing; a model answer with ``status: ok``
never stands in for criteria PASS evidence.
"""

import pytest
from fastapi.testclient import TestClient

from lab import role_service
from lab.role_service import MAX_CONTEXT_BYTES, NOT_ACCEPTANCE, _collect_context

HEADERS = {"x-lab-executor-token": "t"}


@pytest.fixture
def service(monkeypatch):
    prompts = []
    monkeypatch.setenv("LAB_ROLE_EXECUTOR_TOKEN", "t")

    def model(model, prompt, lease, limits):
        prompts.append(prompt)
        return {"response": "Реалізацію перевірено.", "eval_count": 3}

    monkeypatch.setattr(role_service, "_call_model", model)
    return TestClient(role_service.create_app()), prompts


def invoke(client, root, paths=None):
    payload = {"kind": "audit"}
    if paths is not None:
        payload["paths"] = paths
    response = client.post("/invoke", headers=HEADERS, json={
        "task": {"kind": "audit", "payload": payload}, "model": "ollama:m", "role": "reviewer",
        "resource_lease": {"lease_id": "l"}, "workspace": {"root": str(root)}})
    assert response.status_code == 200
    return response.json()


def test_empty_paths_is_listing_only_and_says_so(service, tmp_path):
    client, prompts = service
    (tmp_path / "a.py").write_text("SOURCE_A")
    answer = invoke(client, tmp_path)
    result = answer["result"]
    assert answer["status"] == "ok"
    assert result["report_only"] is True
    assert result["code_examined"] is False and result["files_read"] == []
    assert result["context"]["listed_files"] == 1
    assert any("лише перелік файлів" in note for note in result["limitations"])
    assert any("не підтверджує реалізацію" in note for note in result["limitations"])
    assert NOT_ACCEPTANCE in result["limitations"]
    assert "SOURCE_A" not in prompts[0]
    assert "лише назви, вміст не прочитано" in prompts[0]
    assert "Прочитаний вміст файлів: жодного" in prompts[0]


def test_unavailable_workspace(service, tmp_path):
    client, prompts = service
    result = invoke(client, tmp_path / "absent", ["a.py"])["result"]
    assert result["context"]["workspace_available"] is False
    assert result["code_examined"] is False
    assert any("Робоча копія недоступна" in note for note in result["limitations"])
    assert "Робоча копія недоступна" in prompts[0]


def test_read_files_are_distinguished_from_listing_and_missing(service, tmp_path):
    client, prompts = service
    (tmp_path / "a.py").write_text("SOURCE_A")
    (tmp_path / "b.py").write_text("SOURCE_B")
    (tmp_path / "pkg").mkdir()
    result = invoke(client, tmp_path, ["a.py", "absent.py", "pkg"])["result"]
    assert result["code_examined"] is True
    assert result["files_read"] == [{"path": "a.py", "chars": 8, "total_chars": 8, "truncated": False}]
    assert result["context"]["listed_files"] == 2
    assert result["context"]["missing"] == ["absent.py", "pkg"]
    assert any("absent.py" in note for note in result["limitations"])
    assert "SOURCE_A" in prompts[0] and "SOURCE_B" not in prompts[0]
    assert "Прочитаний вміст файлів: a.py." in prompts[0]


def test_context_limit_truncation_and_unread_files(tmp_path):
    (tmp_path / "big.py").write_text("x" * (MAX_CONTEXT_BYTES + 10))
    (tmp_path / "next.py").write_text("NEXT")
    _, coverage = _collect_context(str(tmp_path), ["big.py", "next.py"])
    assert coverage["read"] == [{"path": "big.py", "chars": MAX_CONTEXT_BYTES,
                                 "total_chars": MAX_CONTEXT_BYTES + 10, "truncated": True}]
    assert coverage["not_read_limit"] == ["next.py"]
    assert any("обрізано лімітом" in note and "big.py" in note for note in coverage["limitations"])
    assert any("вичерпаний ліміт" in note and "next.py" in note for note in coverage["limitations"])


def test_listing_truncation_is_reported(tmp_path):
    for index in range(role_service.MAX_LISTED_FILES + 3):
        (tmp_path / f"f{index}.txt").write_text("")
    context, coverage = _collect_context(str(tmp_path), [])
    assert coverage["listing_truncated"] is True
    assert coverage["listed_files"] == role_service.MAX_LISTED_FILES + 3
    assert any("Перелік файлів обрізано" in note for note in coverage["limitations"])


def test_containment_is_kept(tmp_path):
    (tmp_path / "secret.txt").write_text("SECRET")
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("code")
    (work / "link.txt").symlink_to(tmp_path / "secret.txt")
    context, coverage = _collect_context(str(work), ["a.py", "../secret.txt", "/etc/hostname", "link.txt"])
    assert "SECRET" not in context
    assert [item["path"] for item in coverage["read"]] == ["a.py"]
    assert coverage["outside_workspace"] == ["../secret.txt", "/etc/hostname", "link.txt"]
    assert any("поза робочою копією" in note for note in coverage["limitations"])


def test_report_result_is_not_accepted_as_criteria_evidence(service, tmp_path, monkeypatch):
    """Existing guard: governance rejects evidence derived from a model report."""
    monkeypatch.setenv("LAB_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setenv("LAB_REVIEW_TRUST_ROOT", str(tmp_path / "trusted-reviewers"))
    from lab.governance import Governance, IssueTransport
    from lab.policy import PolicyGate

    client, _ = service
    (tmp_path / "a.py").write_text("code")
    answer = invoke(client, tmp_path, ["a.py"])
    assert answer["status"] == "ok"
    governance = Governance(PolicyGate(tmp_path / "state", activated=True), IssueTransport(dry_run=True),
                            state_dir=tmp_path / "gov", repository="owner/repo")
    verdict = governance.submit_ir_evidence("ir.0.0.0.1", {
        "version": "0.0.3.72", "commit": "77c8b5d5", "environment": "lab",
        "expected": "критерії виконано", "actual": answer["result"]["summary"],
        "reviewer": "reviewer", "report": answer["result"]})
    assert verdict["accepted"] is False
    assert any("test_results" in problem for problem in verdict["problems"])
