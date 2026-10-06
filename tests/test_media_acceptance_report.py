"""Issue #82 - the media acceptance report must never report an unproven row."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "media-acceptance-report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("media_acceptance_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = _load_module()


def test_rows_match_the_declared_acceptance_matrix():
    source = (Path(__file__).parent / "test_full_media_harness.py").read_text(encoding="utf-8")
    declared = {line.split("``")[1] for line in source.splitlines()
                if line.startswith("* ``")}
    assert declared == set(report.ROWS), (
        "the report rows and the harness rows must stay in sync: "
        f"{sorted(declared ^ set(report.ROWS))}")


def test_missing_evidence_is_never_a_pass(tmp_path):
    verdict = report.evaluate(None)
    assert verdict["summary"] == report.NOT_RUN
    assert all(row["status"] == report.NOT_RUN for row in verdict["rows"].values())


def test_partial_evidence_is_not_run_not_a_pass():
    verdict = report.evaluate({"lease": {"job_id": "job-1"}})
    assert verdict["rows"]["lease"]["status"] == report.PASS
    assert verdict["rows"]["cancel"]["status"] == report.NOT_RUN
    assert verdict["summary"] == report.NOT_RUN


def test_a_tampered_artifact_fails_the_row(tmp_path):
    video = tmp_path / "video-v1.mp4"
    video.write_bytes(b"ftypmp42")
    recorded = hashlib.sha256(b"ftypmp42").hexdigest()
    verdict = report.evaluate({"first_run": {"job_id": "job-2", "video": {
        "versions": [{"version": 1, "sha256": recorded}],
        "files": {str(video): recorded},
    }}})
    assert verdict["rows"]["first_run"]["status"] == report.PASS

    video.write_bytes(b"ftypmp42 tampered")
    verdict = report.evaluate({"first_run": {"job_id": "job-2", "video": {
        "versions": [{"version": 1, "sha256": recorded}],
        "files": {str(video): recorded},
    }}})
    assert verdict["rows"]["first_run"]["status"] == report.FAIL
    assert verdict["summary"] == report.FAIL
    assert "does not match" in verdict["rows"]["first_run"]["detail"]


def test_a_deleted_artifact_fails_the_row(tmp_path):
    video = tmp_path / "video-v1.mp4"
    video.write_bytes(b"ftypmp42")
    recorded = hashlib.sha256(b"ftypmp42").hexdigest()
    video.unlink()
    verdict = report.evaluate({"cancel": {"video": {"files": {str(video): recorded}}}})
    assert verdict["rows"]["cancel"]["status"] == report.FAIL
    assert "missing on disk" in verdict["rows"]["cancel"]["detail"]


def test_markdown_renders_every_row(tmp_path):
    verdict = report.evaluate({"lease": {"job_id": "job-3"}})
    markdown = report.render_markdown(verdict, "0.0.2.41")
    assert "Verdict: **NOT_RUN**" in markdown
    for name in report.ROWS:
        assert f"`{name}`" in markdown
    assert "job-3" in markdown


def test_provenance_is_taken_from_the_rows():
    provenance = {"sha": "a" * 40, "version": "0.0.2.41", "dirty": False,
                  "python": "3.12.4", "platform": "Linux",
                  "config": {"backends": {"llm": {"backend": "ollama",
                                                  "configured": True}}}}
    verdict = report.evaluate({"lease": {"job_id": "job-6",
                                         "provenance": provenance}})
    assert verdict["provenance"]["sha"] == "a" * 40
    markdown = report.render_markdown(verdict, "0.0.2.41")
    assert f"SHA: `{'a' * 40}`" in markdown
    assert "Version: `0.0.2.41`" in markdown
    assert "Working tree at run time: clean" in markdown
    assert "Python: `3.12.4` on `Linux`" in markdown
    assert "| llm | `ollama` | yes |" in markdown


def test_rows_from_different_runs_are_refused():
    with pytest.raises(ValueError, match="different runs"):
        report.evaluate({
            "lease": {"job_id": "a", "provenance": {"sha": "a" * 40}},
            "cancel": {"job_id": "b", "provenance": {"sha": "b" * 40}},
        })


def test_expected_and_actual_are_rendered(tmp_path):
    verdict = report.evaluate({"lease": {"job_id": "job-7",
                                         "expected": 409, "actual": 409}})
    row = verdict["rows"]["lease"]
    assert (row["expected"], row["actual"]) == (409, 409)
    markdown = report.render_markdown(verdict, "0.0.2.41")
    assert "| `lease` | PASS | 409 | 409 |" in markdown


def test_expected_and_actual_never_render_as_a_markdown_break():
    verdict = report.evaluate({"lease": {"job_id": "job-8",
                                         "expected": "a|b", "actual": "c\nd"}})
    markdown = report.render_markdown(verdict, "0.0.2.41")
    assert "a\\|b" in markdown
    assert "c d" in markdown


def test_a_long_digest_is_abbreviated_in_the_table_but_kept_in_json():
    digest = "562ad9109f3069a25ff32dcab94f7f5538001dc580e7411ef1d8604ff33fc580"
    verdict = report.evaluate({"evidence": {"expected": digest, "actual": digest}})
    markdown = report.render_markdown(verdict, "0.0.2.41")
    assert digest[:16] in markdown and digest[-8:] in markdown
    assert digest not in markdown
    assert verdict["rows"]["evidence"]["actual"] == digest


def test_missing_provenance_is_reported_as_unknown():
    verdict = report.evaluate({"lease": {"job_id": "job-9"}})
    assert verdict["provenance"] == {}
    markdown = report.render_markdown(verdict, "0.0.2.41")
    assert "SHA: `unknown`" in markdown


def test_cli_fails_when_the_rows_come_from_different_runs(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "lease": {"provenance": {"sha": "a" * 40}},
        "cancel": {"provenance": {"sha": "b" * 40}},
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence", str(evidence), "--require-all"],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "different runs" in result.stderr


def test_cli_writes_json_and_markdown_and_fails_the_gate(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"lease": {"job_id": "job-4"}}), encoding="utf-8")
    out = tmp_path / "report" / "media-acceptance"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence", str(evidence),
         "--out", str(out), "--require-all"],
        capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    document = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert document["summary"] == report.NOT_RUN
    assert out.with_suffix(".md").is_file()


def test_cli_passes_when_every_row_is_present(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({name: {"job_id": "job-5"} for name in report.ROWS}),
                        encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence", str(evidence), "--require-all"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["summary"] == report.PASS
    assert all(row["status"] == report.PASS for row in document["rows"].values())


def test_cli_rejects_an_unreadable_evidence_file(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{not json", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence", str(evidence), "--require-all"],
        capture_output=True, text=True)
    assert result.returncode == 1
    document = json.loads(result.stdout)
    assert document["summary"] == report.NOT_RUN


def test_cli_requires_an_evidence_source():
    import os

    env = {key: value for key, value in os.environ.items() if key != "VERTEP_MEDIA_EVIDENCE"}
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                            env=env)
    assert result.returncode != 0
    assert "--evidence" in result.stderr


@pytest.mark.parametrize("row", report.ROWS)
def test_every_declared_row_is_reported(row):
    verdict = report.evaluate({})
    assert row in verdict["rows"]
