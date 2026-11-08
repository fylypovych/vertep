"""#439: a reader holding the target must not make a Windows write fail."""
import threading
import time

import pytest

from core import atomic_write


def test_a_refused_replace_is_retried_until_the_reader_lets_go(tmp_path, monkeypatch):
    target = tmp_path / "workers.json"
    target.write_text("{}", encoding="utf-8")
    refusals = [PermissionError(5, "Access is denied")] * 3
    real = type(target).replace

    def replace(self, path):
        if refusals:
            raise refusals.pop()
        return real(self, path)

    monkeypatch.setattr(atomic_write.os, "name", "nt")
    monkeypatch.setattr(type(target), "replace", replace)
    atomic_write.atomic_write_json(target, {"a": 1})
    assert target.read_text(encoding="utf-8").startswith("{")
    assert '"a": 1' in target.read_text(encoding="utf-8")


def test_a_lasting_refusal_still_fails(tmp_path, monkeypatch):
    target = tmp_path / "workers.json"
    monkeypatch.setattr(atomic_write.os, "name", "nt")
    monkeypatch.setattr(atomic_write.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(type(target), "replace",
                        lambda self, path: (_ for _ in ()).throw(PermissionError(5, "Access is denied")))
    with pytest.raises(PermissionError):
        atomic_write.atomic_write_json(target, {"a": 1})


def test_posix_does_not_retry(tmp_path, monkeypatch):
    target = tmp_path / "workers.json"
    calls = []
    monkeypatch.setattr(atomic_write.os, "name", "posix")
    monkeypatch.setattr(type(target), "replace",
                        lambda self, path: calls.append(1) or (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):
        atomic_write.atomic_write_json(target, {"a": 1})
    assert calls == [1]


@pytest.mark.skipif(__import__("os").name != "nt", reason="the open-file refusal exists only on Windows")
def test_a_real_open_reader_on_windows(tmp_path):
    target = tmp_path / "workers.json"
    target.write_text("{}", encoding="utf-8")
    handle = open(target, encoding="utf-8")
    threading.Timer(0.2, handle.close).start()
    atomic_write.atomic_write_json(target, {"a": 1})
    time.sleep(0.3)
    assert '"a": 1' in target.read_text(encoding="utf-8")
