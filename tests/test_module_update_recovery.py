"""#513: snapshots, write lock, repeat delivery, exactly one retry, rollback points,
retention and archive/purge — real module schema on a disposable PostgreSQL."""

import json

import psycopg
import pytest

from core.module.lifecycle import LifecycleError
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)
from tests.test_module_lifecycle import M1, M2, _crash, key, make, module_url, package, rows, tables  # noqa: F401

M3 = "CREATE TABLE notes3 (id serial PRIMARY KEY)"
M4 = "CREATE TABLE notes4 (id serial PRIMARY KEY)"
M5 = "CREATE TABLE notes5 (id serial PRIMARY KEY)"


def insert(url, body):
    with psycopg.connect(url, client_encoding="UTF8") as connection:
        connection.execute("INSERT INTO items (body) VALUES (%s)", (body,))


def installed(tmp_path, key, dsn, **options):
    lifecycle = make(tmp_path, key, dsn, **options)
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    return lifecycle, module_url(dsn, lifecycle.credentials["notes"])


def test_writes_are_locked_after_drain_and_open_again_after_the_verified_state(tmp_path, key, module_postgres):
    seen = {}

    def fault(point):
        if point == "after_record":
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                insert(url, "during the update")
            seen["locked"] = lifecycle.database.writes_locked("notes")
    lifecycle, url = installed(tmp_path, key, module_postgres, fault=fault)
    insert(url, "before")
    lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2}), confirmed=True)
    assert seen == {"locked": True} and not lifecycle.database.writes_locked("notes")
    insert(url, "after")
    assert rows(url, "SELECT body FROM items ORDER BY id") == [("before",), ("after",)]


def test_a_repeated_delivery_returns_the_stored_result_without_a_second_apply(tmp_path, key, module_postgres):
    lifecycle, url = installed(tmp_path, key, module_postgres)
    update = package(key, version="1.1.0", migrations={"m1": M1, "m2": M2})
    first = lifecycle.update(update, confirmed=True, operation_id="op-1")
    again = lifecycle.update(update, confirmed=True, operation_id="op-1")
    assert again == first and first["operation"] == "op-1"
    assert rows(module_postgres, "SELECT id FROM mod_notes._vertep_migrations ORDER BY id") == [("m1",), ("m2",)]


def test_local_settings_survive_an_update(tmp_path, key, module_postgres):
    lifecycle, _ = installed(tmp_path, key, module_postgres)
    settings = lifecycle.data_dir("notes") / "config" / "settings.json"
    settings.write_text('{"retention_days": 7}')
    lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2}), confirmed=True)
    assert json.loads(settings.read_text()) == {"retention_days": 7}


def test_one_automatic_retry_then_stop_and_tell_the_administrator(tmp_path, key, module_postgres):
    lifecycle, url = installed(tmp_path, key, module_postgres)
    insert(url, "before")
    _crash(tmp_path, key, module_postgres, "after_apply", "update")
    notes = []
    # The new version keeps failing its check; the previous one is healthy.
    restarted = make(tmp_path, key, module_postgres, self_test=lambda manifest: manifest.version == "1.0.0",
                     notify=lambda message, details: notes.append((message, details["module_id"])))
    assert restarted.recover() == [{"module_id": "notes", "op": "update", "outcome": "retry_failed"}]
    assert notes == [("Module update failed after its automatic retry", "notes")]
    assert restarted.record("notes")["version"] == "1.0.0"
    assert tables(module_postgres, "mod_notes") == ["_vertep_migrations", "items"]
    assert not restarted.database.writes_locked("notes")  # previous version verified: writes open
    assert rows(url, "SELECT body FROM items") == [("before",)]
    assert restarted.recover() == []  # exactly one retry, nothing runs again


def test_an_unverified_restore_keeps_writes_locked_and_stops(tmp_path, key, module_postgres):
    lifecycle, _ = installed(tmp_path, key, module_postgres)
    _crash(tmp_path, key, module_postgres, "after_apply", "update")
    journal = json.loads((tmp_path / "lc/journal/notes.json").read_text())
    (tmp_path / "lc" / "snapshots" / "notes" / journal["operation"] / "checksum.json").write_text('{"snapshot": "0", "files": "0"}')
    notes = []
    restarted = make(tmp_path, key, module_postgres, notify=lambda message, details: notes.append(message))
    assert restarted.recover() == [{"module_id": "notes", "op": "update", "outcome": "restore_failed"}]
    assert notes == ["Module update stopped: restore was not verified"]
    assert restarted.database.writes_locked("notes")  # unknown state: no writes until the administrator acts
    assert restarted.record("notes")["attention"]["operation"] == journal["operation"]


def test_manual_rollback_lists_suitable_points_and_needs_data_loss_confirmation(tmp_path, key, module_postgres):
    lifecycle, url = installed(tmp_path, key, module_postgres)
    insert(url, "kept")
    lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2}), confirmed=True)
    insert(url, "later write")
    points = lifecycle.rollback_points("notes")
    assert [p["version"] for p in points] == ["1.0.0"] and points[0]["compatible"] and points[0]["created_at"]
    with pytest.raises(LifecycleError, match="drops later writes"):
        lifecycle.rollback_to_point("notes", points[0]["operation"])
    restored = lifecycle.rollback_to_point("notes", points[0]["operation"], accept_data_loss=True)
    assert restored["version"] == "1.0.0"
    assert rows(url, "SELECT body FROM items") == [("kept",)]
    with pytest.raises(LifecycleError, match="No suitable"):
        lifecycle.rollback_to_point("notes", "unknown-operation", accept_data_loss=True)


def test_retention_keeps_the_active_and_two_previous_versions(tmp_path, key, module_postgres):
    lifecycle, _ = installed(tmp_path, key, module_postgres)
    migrations = {"m1": M1}
    for version, (mid, sql) in zip(("1.1.0", "1.2.0", "1.3.0", "1.4.0"),
                                   (("m2", M2), ("m3", M3), ("m4", M4), ("m5", M5))):
        migrations = {**migrations, mid: sql}
        lifecycle.update(package(key, version=version, migrations=migrations), confirmed=True)
    versions = sorted(json.loads((path / "snapshot.json").read_text())["version"]
                      for path in (tmp_path / "lc" / "snapshots" / "notes").iterdir())
    # Snapshots hold the state before each update: 1.3.0 (previous) and 1.2.0; 1.0.0/1.1.0 pruned.
    assert versions == ["1.2.0", "1.3.0"]
    assert lifecycle.record("notes")["previous"]["version"] == "1.3.0"  # still restorable


def test_uninstall_keeps_a_restorable_archive_and_purge_is_a_separate_action(tmp_path, key, module_postgres):
    lifecycle, url = installed(tmp_path, key, module_postgres)
    insert(url, "archived")
    with pytest.raises(LifecycleError, match="Uninstall the module first"):
        lifecycle.purge("notes", confirmed=True)
    archive = lifecycle.uninstall("notes", confirmed=True)["archive"]
    other = tmp_path / "lc" / "archives" / "notes-extra-1.0.0-x.tar"  # another module with a longer id
    other.write_bytes(b"not ours")
    restored = lifecycle.restore_archive(tmp_path / "lc" / "archives" / archive)
    assert restored["state"] == "INSTALLED"
    lifecycle.uninstall("notes", confirmed=True)
    with pytest.raises(LifecycleError, match="confirmation"):
        lifecycle.purge("notes", confirmed=False)
    purged = lifecycle.purge("notes", confirmed=True)
    assert len(purged["purged"]) == 2 and other.exists()
