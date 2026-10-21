"""Tests of the laboratory qualification evidence model (Issue #252).

The harness may only claim what it has really observed.  These tests prove
that the validator rejects mocked, skipped, incomplete or secret-bearing
evidence and that the collectors never invent a fact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.qualification import (CRITERIA, CRITERIA_BY_ID, CRITERION_IDS, STATUS_BLOCKED,
                               STATUS_FAIL, STATUS_MOCK, STATUS_PASS, STATUS_SKIPPED,
                               build_document, build_row, find_secret, overall_status,
                               summarize, validate_evidence, validate_row)
from lab.qualification_run import (QualificationContext, collect_artifact_facts,
                                   collect_gpu_passthrough_facts, collect_host_facts,
                                   collect_isolation_facts, collect_version_facts,
                                   default_evidence_dir, run_r2, run_r5, run_r6)

SCRIPT = ROOT / "scripts" / "qualify_lab.py"


def _facts(criterion_id: str) -> dict:
    criterion = CRITERIA_BY_ID[criterion_id]
    facts = {
        "version": "0.0.2.70",
        "git_sha": "a" * 40,
        "image_digests": [{"image": "ghcr.io/fylypovych/vertep-lab:0.0.2.70",
                           "id": "sha256:" + "b" * 64}],
        "environment": {"host": {"cpu_count": 8, "os_release": {"ID": "ubuntu"}}},
        "audit": {"records": 3, "valid": True},
        "metrics": {"problems": []},
    }
    for key in criterion.required_facts:
        facts.setdefault(key, {"observed": True})
    return facts


def _passing_row(criterion_id: str = "R1") -> dict:
    return build_row(
        criterion_id,
        status=STATUS_PASS,
        actual="observed on the stand",
        facts=_facts(criterion_id),
        artifacts=[{"path": "r1-lifecycle.txt", "sha256": "c" * 64, "bytes": 128}],
        verification={"verifier": "verification-agent", "collector": "qualify-lab",
                      "method": "independent re-check of the hashed artifacts"},
    )


def _context(tmp_path: Path, **overrides) -> QualificationContext:
    context = QualificationContext(
        repo_root=ROOT,
        evidence_dir=tmp_path / "evidence",
        state_dir=tmp_path / "state",
    )
    for key, value in overrides.items():
        setattr(context, key, value)
    context.evidence_dir.mkdir(parents=True, exist_ok=True)
    context.state_dir.mkdir(parents=True, exist_ok=True)
    return context


class TestCatalog:
    def test_catalog_covers_the_whole_matrix(self):
        assert CRITERION_IDS == ("R1", "R2", "R3", "R4", "R5", "R6")

    def test_every_criterion_is_complete(self):
        for criterion in CRITERIA:
            assert criterion.expected.strip()
            assert criterion.required_facts
            assert criterion.required_rights
            assert criterion.source.strip()

    def test_expected_text_is_bound_to_the_catalog(self):
        row = _passing_row("R2")
        row["expected"] = "something else"
        problems = validate_row(row)
        assert any("expected text" in problem for problem in problems)


class TestRowValidation:
    def test_complete_row_is_valid(self):
        assert validate_row(_passing_row()) == []

    def test_missing_fact_is_rejected(self):
        row = _passing_row()
        row["facts"].pop("version")
        problems = validate_row(row)
        assert any("version" in problem for problem in problems)

    def test_short_sha_is_rejected(self):
        row = _passing_row()
        row["facts"]["git_sha"] = "abc123"
        problems = validate_row(row)
        assert any("git_sha" in problem for problem in problems)

    def test_artifact_without_hash_is_rejected(self):
        row = _passing_row()
        row["artifacts"] = [{"path": "r1-lifecycle.txt"}]
        problems = validate_row(row)
        assert any("sha256" in problem for problem in problems)

    def test_row_without_artifacts_is_rejected(self):
        row = _passing_row()
        row["artifacts"] = []
        problems = validate_row(row)
        assert any("hashed artifact" in problem for problem in problems)

    def test_same_actor_may_not_verify_itself(self):
        row = _passing_row()
        row["verification"]["verifier"] = row["verification"]["collector"]
        problems = validate_row(row)
        assert any("same actor" in problem for problem in problems)

    def test_skipped_and_mocked_rows_are_never_a_proof(self):
        for status in (STATUS_SKIPPED, STATUS_MOCK):
            row = _passing_row()
            row["status"] = status
            problems = validate_row(row)
            assert any(status in problem for problem in problems)

    def test_blocked_row_does_not_claim_any_fact(self):
        row = build_row("R1", status=STATUS_BLOCKED,
                        actual="no stand available: the owner must provide the VM")
        assert validate_row(row) == []

    def test_unknown_criterion_is_rejected(self):
        row = _passing_row()
        row["criterion"] = "R9"
        problems = validate_row(row)
        assert problems == ["unknown criterion: 'R9'"]

    def test_secret_in_evidence_is_rejected(self):
        row = _passing_row()
        row["facts"]["environment"]["notes"] = "password = Sup3rSecretValue"
        problems = validate_row(row)
        assert any("secret" in problem for problem in problems)

    def test_find_secret_ignores_placeholders(self):
        assert find_secret("token: <redacted>") is None
        assert find_secret("api_key: [masked]") is None
        assert find_secret("ghp_abcdefghijklmnopqrstuvwxyz012345") == "github_token"


class TestEvidenceMatrix:
    def test_missing_criterion_is_incomplete(self):
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS[:-1]]
        verdict = validate_evidence(rows)
        assert verdict["missing"] == ["R6"]
        assert overall_status(rows) == "INCOMPLETE"

    def test_every_row_is_required_for_a_pass(self):
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS]
        assert overall_status(rows) == STATUS_PASS

    def test_broken_row_prevents_a_pass(self):
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS]
        rows[0]["facts"].pop("version")
        assert overall_status(rows) == "INCOMPLETE"

    def test_failed_row_is_reported_as_failed(self):
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS]
        rows[1]["status"] = STATUS_FAIL
        assert overall_status(rows) == STATUS_FAIL

    def test_blocked_row_is_reported_as_blocked(self):
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS]
        rows[2]["status"] = STATUS_BLOCKED
        assert overall_status(rows) == STATUS_BLOCKED

    def test_duplicate_criterion_is_rejected(self):
        rows = [_passing_row("R1"), _passing_row("R1")]
        verdict = validate_evidence(rows)
        assert any("2 rows" in problem for problem in verdict["problems"])

    def test_document_carries_summary_and_verdict(self):
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS]
        document = build_document(rows, context={"stand": "test"})
        assert document["overall"] == STATUS_PASS
        assert document["summary"]["total"] == 6
        assert document["summary"]["passed"] == 6
        assert document["context"]["stand"] == "test"

    def test_summary_counts_every_status(self):
        rows = [_passing_row("R1"), _passing_row("R2")]
        rows[1]["status"] = STATUS_BLOCKED
        summary = summarize(rows)
        assert summary["passed"] == 1
        assert summary["blocked"] == 1
        assert summary["total"] == 2


class TestCollectors:
    def test_version_facts_report_the_full_sha(self):
        facts = collect_version_facts(ROOT)
        assert len(facts["git_sha"]) == 40
        assert facts["version"] == (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    def test_artifact_facts_hash_the_real_file(self, tmp_path):
        target = tmp_path / "artifact.json"
        target.write_text('{"at": "2026-10-21T00:00:00+00:00"}', encoding="utf-8")
        entries = collect_artifact_facts([target])
        assert entries[0]["exists"] is True
        assert entries[0]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()

    def test_missing_artifact_is_reported(self, tmp_path):
        entries = collect_artifact_facts([tmp_path / "absent.txt"])
        assert entries[0]["exists"] is False
        assert entries[0]["sha256"] == ""

    def test_isolation_facts_carry_the_expectation(self):
        entries = collect_isolation_facts(["/var/run/docker.sock", "/opt/vertep/config"])
        expectations = {entry["path"]: entry["expectation"] for entry in entries}
        assert expectations["/var/run/docker.sock"] == "unreadable"
        assert expectations["/opt/vertep/config"] == "unwritable"

    def test_host_facts_report_cpu_and_memory(self):
        facts = collect_host_facts()
        assert facts["cpu_count"] >= 1
        assert "container" in facts

    def test_gpu_passthrough_is_observed(self):
        facts = collect_gpu_passthrough_facts()
        assert "passthrough" in facts
        assert isinstance(facts["devices"], list)

    def test_evidence_directory_stays_outside_the_repository(self):
        configured = default_evidence_dir()
        assert ROOT not in configured.parents


class TestRunners:
    def test_r2_without_an_endpoint_is_blocked(self, tmp_path):
        context = _context(tmp_path)
        row = run_r2(context)
        assert row["status"] == STATUS_BLOCKED
        assert "LAB_VERTEP_URL" in row["actual"]

    def test_r2_without_owner_activation_is_blocked(self, tmp_path):
        context = _context(tmp_path, vertep_url="http://127.0.0.1:9", idle_sharing_enabled=False)
        row = run_r2(context)
        assert row["status"] == STATUS_BLOCKED
        assert "LAB_IDLE_SHARING_ENABLED" in row["actual"]

    def test_r5_waits_for_the_previous_rows(self, tmp_path):
        context = _context(tmp_path)
        rows = [_passing_row("R1"), _passing_row("R2")]
        rows[1]["status"] = STATUS_BLOCKED
        row = run_r5(context, rows)
        assert row["status"] == STATUS_BLOCKED
        assert "R2" in row["actual"]

    def test_r6_requires_independent_verification(self, tmp_path):
        context = _context(tmp_path)
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS[:-1]]
        row = run_r6(context, rows)
        assert row["status"] == STATUS_BLOCKED
        assert "verification.json" in row["actual"]

    def test_r6_accepts_an_independent_verification_record(self, tmp_path):
        context = _context(tmp_path)
        (context.evidence_dir / "verification.json").write_text(
            json.dumps({"verifier": "verification-agent", "collector": "qualify-lab",
                        "method": "independent re-check"}), encoding="utf-8")
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS[:-1]]
        row = run_r6(context, rows)
        assert row["status"] == STATUS_PASS
        assert row["artifacts"][0]["sha256"]


class TestCommandLine:
    def _run(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=300)

    def test_plan_is_not_a_proof(self, tmp_path):
        result = self._run("--plan", "--evidence-dir", str(tmp_path / "evidence"),
                           "--state-dir", str(tmp_path / "state"))
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert [item["id"] for item in payload["criteria"]] == list(CRITERION_IDS)
        assert payload["mode"] == "plan"

    def test_plan_only_is_explicit(self, tmp_path):
        result = self._run("--plan", "--plan-only", "--evidence-dir",
                           str(tmp_path / "evidence"), "--state-dir", str(tmp_path / "state"))
        assert result.returncode == 0

    def test_validate_rejects_an_incomplete_document(self, tmp_path):
        document = tmp_path / "evidence.json"
        document.write_text(json.dumps({"rows": [_passing_row("R1")]}), encoding="utf-8")
        result = self._run("--validate", str(document))
        assert result.returncode == 1
        assert json.loads(result.stdout)["overall"] == "INCOMPLETE"

    def test_validate_accepts_a_complete_document(self, tmp_path):
        document = tmp_path / "evidence.json"
        rows = [_passing_row(criterion) for criterion in CRITERION_IDS]
        document.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        result = self._run("--validate", str(document))
        assert result.returncode == 0
        assert json.loads(result.stdout)["overall"] == STATUS_PASS

    def test_validate_reports_an_unreadable_document(self, tmp_path):
        result = self._run("--validate", str(tmp_path / "absent.json"))
        assert result.returncode == 2

    def test_collect_never_claims_a_pass_without_a_stand(self, tmp_path):
        result = self._run("--collect", "--evidence-dir", str(tmp_path / "evidence"),
                           "--state-dir", str(tmp_path / "state"),
                           "--output", str(tmp_path / "evidence.json"))
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["overall"] != STATUS_PASS
        statuses = {row["criterion"]: row["status"] for row in payload["rows"]}
        assert statuses["R2"] == STATUS_BLOCKED
        assert statuses["R5"] == STATUS_BLOCKED
        document = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
        assert document["issue"] == "ir.0.0.2.52"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
