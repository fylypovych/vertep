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
    ws = Workspace(Path(tempfile.mkdtemp()) / "lab-copy", environ={"DATABASE_URL": "x"})
    with pytest.raises(IsolationViolation):
        ws.require_isolated()


def test_forbidden_production_path_is_rejected():
    ws = Workspace(Path("/opt/vertep"), environ={})
    report = ws.verify()
    assert report.isolated is False