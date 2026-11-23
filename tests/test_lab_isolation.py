"""Workspace isolation proofs for the laboratory (Issue i.0.0.2.50, L8)."""

import os
import tempfile
from pathlib import Path

import pytest

from lab.workspace import Workspace, IsolationViolation


def test_isolated_working_copy_passes():
    root = Path(tempfile.mkdtemp()) / "lab-copy"
    root.mkdir()
    ws = Workspace(root, environ={})
    ws.prepare()
    ws.require_isolated()
    assert ws.verify().isolated is True


def test_production_environment_is_rejected():
    root = Path(tempfile.mkdtemp()) / "lab-copy"
    root.mkdir()  # an existing copy: only the production variable can be the reason
    ws = Workspace(root, environ={"DATABASE_URL": "x"})
    with pytest.raises(IsolationViolation, match="DATABASE_URL"):
        ws.require_isolated()


def test_forbidden_production_path_is_rejected(monkeypatch):
    root = Path(tempfile.mkdtemp()) / "production-tree"
    root.mkdir()  # exists, so the refusal comes from the production check
    monkeypatch.setenv("VERTEP_PRODUCTION_ROOT", str(root))
    report = Workspace(root, environ={}).verify()
    assert report.isolated is False
    assert any("points at the production repository" in problem for problem in report.problems)