"""#362: only the remaining role/state/resource cells; existing cells are reused."""
import pytest

from tests.test_system_state_mutation_matrix import actual_backend, PROBES, _request
from tests.acceptance_provenance import acceptance_provenance
from core.system_state import SystemState, set_system_state, _path, get_system_state


REMAINING = [("admin", "RECOVERING", 423), ("viewer", "READ_ONLY", 423),
             ("viewer", "RECOVERING", 423)]


@pytest.mark.parametrize("role,mode,status", REMAINING)
@pytest.mark.parametrize("resource", sorted(PROBES))
def test_remaining_resource_cells_preserve_actual_state(actual_backend, role, mode, status, resource):
    probe = PROBES[resource](actual_backend)
    set_system_state(SystemState(mode), "remaining access matrix")
    before = probe["snapshot"]()
    response = _request(actual_backend, probe["method"], probe["path"], probe["body"], role=role)
    assert response.status_code == status, response.text
    assert probe["snapshot"]() == before


@pytest.mark.parametrize("record", ['{"state":"FUTURE_UNKNOWN_MODE"}', '{"state":', '{}', '[]', 'null'])
def test_unknown_persisted_state_does_not_enable_mutation(actual_backend, record):
    set_system_state(SystemState.NORMAL, "unknown record precondition")
    path = _path()
    path.write_text(record, encoding="utf-8")
    probe = PROBES["brands"](actual_backend)
    before = probe["snapshot"]()
    response = _request(actual_backend, probe["method"], probe["path"], probe["body"])
    assert response.status_code == 423, response.text
    assert probe["snapshot"]() == before
    assert get_system_state()["state"] == "EMERGENCY"
