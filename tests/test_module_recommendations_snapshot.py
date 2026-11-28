"""#553 AC5: the recommendations snapshot survives between API requests (a new ModuleSources each)."""

import time
from types import SimpleNamespace

from core.module.sources import ModuleSources


def test_snapshot_is_shared_by_new_instances_and_reset_by_a_new_pin(tmp_path, monkeypatch):
    lifecycle = SimpleNamespace(root=tmp_path)
    calls = []
    monkeypatch.setattr(ModuleSources, "recommendations", lambda self: calls.append(1) or [{"module_id": "notes"}])
    first = ModuleSources(lifecycle).recommendations_snapshot(refresh=True)
    assert first["recommendations"] == [{"module_id": "notes"}] and not first["refreshing"]
    cached = ModuleSources(lifecycle).recommendations_snapshot()  # another request: served from the cache
    assert cached["recommendations"] == [{"module_id": "notes"}] and not cached["stale"] and len(calls) == 1
    ModuleSources(lifecycle)._snapshot_state()["checked_at"] = None
    ModuleSources(lifecycle).recommendations_snapshot()  # out of date: a background refresh starts
    deadline = time.monotonic() + 5
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(calls) == 2
