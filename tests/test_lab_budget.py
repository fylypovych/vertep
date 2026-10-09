"""Run, quota and model-cost proofs (Issue #107, stage 2).

The laboratory must not burn paid API quota and must pause in a controlled way
when its limits are exhausted, so the same budget can be reused across
restarts without losing state.
"""

import pytest

from lab.budget import (BudgetLedger, LabPaused, PaidModelDenied, QuotaExceeded)


@pytest.fixture()
def ledger(tmp_path):
    return BudgetLedger(tmp_path / "state")


def test_allow_list_allows_free_models(ledger):
    assert ledger.allow_model("ollama", "qwen2.5-coder") is True
    ledger.allowed_providers = ("local",)
    assert ledger.allow_model("local") is True


def test_paid_providers_are_refused(ledger):
    for provider in ("openai", "anthropic", "gemini", "groq-paid", "paid"):
        with pytest.raises(PaidModelDenied):
            ledger.require_model(provider)


def test_unknown_provider_is_refused(ledger):
    with pytest.raises(PaidModelDenied):
        ledger.require_model("openai", "gpt-4o")


def test_quota_exceeded_blocks_new_runs(ledger):
    ledger.max_runs_per_day = 1
    ledger.reserve_run()
    with pytest.raises(QuotaExceeded):
        ledger.reserve_run()


def test_pause_keeps_running_until_it_expires(ledger):
    ledger.pause(3600, reason="rate limit")
    with pytest.raises(LabPaused):
        ledger.reserve_run()
    # Advance the clock past the pause so a restart re-enables the lab.
    from datetime import datetime, timezone
    ledger.pause_until = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    assert ledger.paused() is False
    assert ledger.usage()["pause_until"] == ""


def test_standalone_usage_report_is_stable_across_restart(tmp_path):
    first = BudgetLedger(tmp_path / "state")
    first.max_runs_per_day = 5
    first.reserve_run()
    second = BudgetLedger(tmp_path / "state")
    assert second.usage()["runs_last_day"] == 1
    assert second.usage()["max_runs_per_day"] == 5


# -- JSONL/journal persistence --------------------------------------------
def test_pauses_and_runs_are_persisted(tmp_path):
    ledger = BudgetLedger(tmp_path / "state")
    ledger.reserve_run()
    ledger.record_run(tokens=123)
    ledger.pause(60, reason="rate limit")
    reloaded = BudgetLedger(tmp_path / "state")
    assert len(reloaded.runs) == 1
    assert reloaded.runs[0]["tokens"] == 123
    pauses = reloaded.pauses()
    assert len(pauses) == 1
    assert pauses[0]["reason"] == "rate limit"
