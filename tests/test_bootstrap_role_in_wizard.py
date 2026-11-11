"""The node role is chosen in the First Run Wizard, never by a bootstrap prompt."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_does_not_ask_for_roles():
    source = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "Select node role" not in source and "Additional roles:" not in source
    assert not re.search(r"read -r -p \"(Role|Additional)", source)
    # Without an automation override the node starts unassigned and the wizard decides.
    assert 'NODE_ROLE=${VERTEP_ROLE:-${existing_node_role:-unassigned}}' in source
