"""i.0.0.4.94 (#494): transactional module lifecycle, data, rollback, recovery, archive.

Runs against a real disposable PostgreSQL (tests/module_postgres.py) and real
files; restart recovery kills a real subprocess mid-update.
"""

import base64
import json
import os
import stat
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.module.data import ModuleDatabase, schema_name
from core.module.instances import ModuleInstanceRegistry, PlacementStore
from core.module.lifecycle import LifecycleError, ModuleLifecycle
from core.module.packages import verify_package
from core.module.trust import Publisher, TrustStore
from tests.module_postgres import module_postgres  # noqa: F401  (fixture)
from tests.test_module_trust_isolation import CONTAINER, pem, sign

ROOT = Path(__file__).resolve().parents[1]
M1 = "CREATE TABLE items (id serial PRIMARY KEY, body text NOT NULL)"
M2 = "CREATE TABLE tags (id serial PRIMARY KEY, item int NOT NULL REFERENCES items(id), tag text NOT NULL)"


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


def package(key, name="notes", version="1.0.0", migrations=None, **extra):
    migrations = migrations if migrations is not None else {"m1": M1}
    manifest = {"schema_version": "2.0", "name": name, "version": version, "capabilities": [], "kind": "application",
                "publisher": "acme", "provenance": {"repository": "github.com/acme/notes"}, "container": CONTAINER,
                "migrations": [{"id": mid, "version": version} for mid in migrations], **extra}
    files = {f"migrations/{mid}.sql": base64.b64encode(sql.encode()).decode() for mid, sql in migrations.items()}
    return sign(key, manifest, files)


def make(tmp_path, key, dsn, **options):
    trust = TrustStore({"acme": Publisher("acme", {"acme-k1": pem(key)}, ["github.com/acme/notes"])})
    registry = ModuleInstanceRegistry({"core_runtime": "0.0.3.88"}, PlacementStore(tmp_path / "placement.json"))
    credentials = {}
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda p: verify_package(p, trust), database=ModuleDatabase(dsn),
                                registry=registry, system_versions={"core_runtime": "0.0.3.88"},
                                store_credentials=lambda module_id, creds: credentials.__setitem__(module_id, creds),
                                drain_timeout=options.pop("drain_timeout", 1.0), **options)
    lifecycle.credentials = credentials
    return lifecycle


def module_url(dsn, credentials):
    base, database = dsn.rsplit("/", 1)
    host = base.split("@", 1)[1]
    return f"postgresql://{credentials['role']}:{credentials['password']}@{host}/{database}"


def rows(dsn, sql):
    with psycopg.connect(dsn, client_encoding="UTF8") as connection:
        return connection.execute(sql).fetchall()


def tables(dsn, schema):
    return sorted(r[0] for r in rows(dsn, f"SELECT tablename FROM pg_tables WHERE schemaname='{schema}'"))


def test_install_activate_update_rollback_uninstall_restore(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key))
    with pytest.raises(LifecycleError, match="installed"):
        lifecycle.install(package(key))
    lifecycle.activate("notes")
    credentials = lifecycle.credentials["notes"]
    url = module_url(module_postgres, credentials)
    with psycopg.connect(url, client_encoding="UTF8") as connection:  # the module's own role and search_path
        connection.execute("INSERT INTO items (body) VALUES ('first'), ('second')")
    config = lifecycle.data_dir("notes") / "config/settings.json"
    config.write_text('{"theme": "dark"}')
    (lifecycle.data_dir("notes") / "files/history.csv").write_text("t,v\n1,2\n")

    with pytest.raises(LifecycleError, match="confirmation"):
        lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2}), confirmed=False)
    lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2}), confirmed=True)
    assert lifecycle.record("notes")["version"] == "1.1.0" and lifecycle.record("notes")["state"] == "ACTIVE"
    assert tables(module_postgres, "mod_notes") == ["_vertep_migrations", "items", "tags"]
    with psycopg.connect(url, client_encoding="UTF8") as connection:
        connection.execute("INSERT INTO tags (item, tag) VALUES (1, 'x')")
    config.write_text('{"theme": "light"}')

    lifecycle.rollback("notes")  # previous working version with its pre-update data/config
    assert lifecycle.record("notes")["version"] == "1.0.0" and lifecycle.record("notes")["state"] == "ACTIVE"
    assert tables(module_postgres, "mod_notes") == ["_vertep_migrations", "items"]
    assert config.read_text() == '{"theme": "dark"}'
    assert [r[0] for r in rows(url, "SELECT body FROM items ORDER BY id")] == ["first", "second"]

    with pytest.raises(LifecycleError, match="confirmation"):
        lifecycle.uninstall("notes", confirmed=False)
    result = lifecycle.uninstall("notes", confirmed=True)
    archive = tmp_path / "lc/archives" / result["archive"]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "lc/archives").stat().st_mode) == 0o700
    with tarfile.open(archive) as tar:  # plain, unencrypted tar
        names = set(tar.getnames())
        index = json.loads(tar.extractfile("archive.json").read())
        assert index["encrypted"] is False
        blob = b"".join(tar.extractfile(n).read() for n in names)
    assert {"database.json", "config/settings.json", "files/history.csv", "migrations/m1.sql", "package.json"} <= names
    assert credentials["password"].encode() not in blob  # secrets are not archived
    assert tables(module_postgres, "mod_notes") == []
    assert not rows(module_postgres, f"SELECT 1 FROM pg_roles WHERE rolname='{credentials['role']}'")
    assert not lifecycle.data_dir("notes").exists() and lifecycle.record("notes") is None

    lifecycle.restore_archive(archive)
    assert lifecycle.record("notes")["state"] == "INSTALLED"
    restored = module_url(module_postgres, lifecycle.credentials["notes"])
    assert [r[0] for r in rows(restored, "SELECT body FROM items ORDER BY id")] == ["first", "second"]
    with psycopg.connect(restored, client_encoding="UTF8") as connection:  # sequences continue after restored rows
        connection.execute("INSERT INTO items (body) VALUES ('third')")
    assert config.read_text() == '{"theme": "dark"}'
    assert archive.exists()  # never deleted by Vertep


@pytest.mark.parametrize("failure", ["migration", "self_test"])
def test_failed_update_rolls_back_runtime_db_and_config(tmp_path, key, module_postgres, failure):
    versions = {"bad": "1.1.0"}
    lifecycle = make(tmp_path, key, module_postgres,
                     self_test=lambda manifest: not (failure == "self_test" and manifest.version == versions["bad"]))
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    url = module_url(module_postgres, lifecycle.credentials["notes"])
    with psycopg.connect(url, client_encoding="UTF8") as connection:
        connection.execute("INSERT INTO items (body) VALUES ('kept')")
    migrations = {"m1": M1, "m2": "CREATE TABLE tags (id int); SELECT 1/0" if failure == "migration" else M2}
    with pytest.raises(Exception):
        lifecycle.update(package(key, version="1.1.0", migrations=migrations), confirmed=True)
    record = lifecycle.record("notes")
    assert (record["version"], record["state"]) == ("1.0.0", "ACTIVE")
    assert tables(module_postgres, "mod_notes") == ["_vertep_migrations", "items"]
    assert rows(url, "SELECT body FROM items") == [("kept",)]
    assert not (tmp_path / "lc/journal/notes.json").exists()


def test_no_false_ready_when_the_restored_version_fails_too(tmp_path, key, module_postgres):
    state = {"healthy": True}
    lifecycle = make(tmp_path, key, module_postgres, self_test=lambda manifest: state["healthy"])
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    lifecycle.registry.report_node("n", [{**package(key)["manifest"], "ready": True, "endpoint": "http://127.0.0.1:9"}])
    lifecycle.registry.set_placement("notes", ["n"])
    assert len(lifecycle.registry.ready_instances("notes")) == 1
    state["healthy"] = False
    with pytest.raises(LifecycleError):
        lifecycle.update(package(key, version="1.1.0"), confirmed=True)
    assert lifecycle.record("notes")["state"] == "FAILED"
    assert lifecycle.registry.ready_instances("notes") == []
    assert "lifecycle_failed" in lifecycle.registry.instances("notes")[0].reasons


RECOVERY_SCRIPT = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from tests.test_module_lifecycle import make
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from pathlib import Path
    key = load_pem_private_key(Path(sys.argv[3]).read_bytes(), None)
    def crash(point):
        if point == sys.argv[5]:
            os._exit(9)  # real process death in the middle of the operation
    lifecycle = make(Path(sys.argv[2]), key, sys.argv[4], fault=crash)
    if sys.argv[6] == "update":
        from tests.test_module_lifecycle import package, M1, M2
        lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2}), confirmed=True)
    else:
        lifecycle.uninstall("notes", confirmed=True)
""")


def _crash(tmp_path, key, dsn, point, operation):
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    script = tmp_path / "crash.py"
    script.write_text(RECOVERY_SCRIPT)
    result = subprocess.run([sys.executable, str(script), str(ROOT), str(tmp_path), str(key_file), dsn, point, operation],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 9, result.stderr


def test_restart_recovery_after_a_killed_update(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    url = module_url(module_postgres, lifecycle.credentials["notes"])
    with psycopg.connect(url, client_encoding="UTF8") as connection:
        connection.execute("INSERT INTO items (body) VALUES ('before')")
    _crash(tmp_path, key, module_postgres, "after_apply", "update")
    assert json.loads((tmp_path / "lc/journal/notes.json").read_text())["phase"] == "applied"
    assert "tags" in tables(module_postgres, "mod_notes")  # the dead process left a half-applied update
    restarted = make(tmp_path, key, module_postgres)
    # #513: the unknown state is restored from the verified snapshot, the previous version is
    # checked, then the approved update is retried exactly once (owner decision in #509).
    assert restarted.recover() == [{"module_id": "notes", "op": "update", "outcome": "retried"}]
    record = restarted.record("notes")
    assert (record["version"], record["state"]) == ("1.1.0", "ACTIVE")
    assert tables(module_postgres, "mod_notes") == ["_vertep_migrations", "items", "tags"]
    assert rows(module_postgres, "SELECT id FROM mod_notes._vertep_migrations ORDER BY id") == [("m1",), ("m2",)]
    assert rows(url, "SELECT body FROM items") == [("before",)]  # no double effect, data kept
    assert restarted.recover() == []  # idempotent, nothing left to recover


def test_restart_recovery_completes_an_uninstall_after_a_verified_archive(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    _crash(tmp_path, key, module_postgres, "after_archive", "uninstall")
    restarted = make(tmp_path, key, module_postgres)
    assert restarted.recover()[0]["outcome"] == "uninstall_completed"
    assert restarted.record("notes") is None and tables(module_postgres, "mod_notes") == []
    assert len(list((tmp_path / "lc/archives").glob("notes-*.tar"))) == 1


def test_archive_failure_blocks_deletion(tmp_path, key, module_postgres, monkeypatch):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    monkeypatch.setattr(lifecycle, "verify_archive", lambda path: (_ for _ in ()).throw(LifecycleError("corrupt")))
    with pytest.raises(LifecycleError, match="blocked"):
        lifecycle.uninstall("notes", confirmed=True)
    assert lifecycle.record("notes")["state"] == "INACTIVE"
    assert tables(module_postgres, "mod_notes") == ["_vertep_migrations", "items"]
    assert lifecycle.data_dir("notes").exists()


def test_tampered_archive_is_rejected(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    archive = lifecycle.create_archive("notes")
    with tarfile.open(archive) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    members["database.json"] = members["database.json"].replace(b"items", b"itemz")
    import io
    with tarfile.open(archive, "w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    with pytest.raises(LifecycleError, match="integrity"):
        lifecycle.verify_archive(archive)


def test_dependency_guards_and_drain(tmp_path, key, module_postgres):
    observed = {"calls": 0, "cancelled": []}
    lifecycle = make(tmp_path, key, module_postgres, drain_timeout=0.3)

    def active(module_id):
        observed["calls"] += 1
        # New dispatch already stopped while tasks drain.
        observed["state_during_drain"] = lifecycle.record(module_id)["state"]
        return ["task-1"]
    lifecycle.active_tasks = active
    lifecycle.cancel_tasks = lambda module_id: observed["cancelled"].append(module_id)
    lifecycle.install(package(key))
    lifecycle.activate("notes")
    dependent = package(key, name="reports", migrations={}, dependencies=[{"name": "notes", "min_version": "1.0.0"}])
    lifecycle.install(dependent)
    lifecycle.activate("reports")
    for action in (lambda: lifecycle.deactivate("notes"), lambda: lifecycle.uninstall("notes", confirmed=True)):
        with pytest.raises(LifecycleError, match="depend"):
            action()
    with pytest.raises(LifecycleError, match="dependencies"):
        lifecycle.update(package(key, name="reports", version="2.0.0", migrations={},
                                 dependencies=[{"name": "notes", "min_version": "9.0.0"}]), confirmed=True)
    with pytest.raises(LifecycleError, match="dependencies"):
        lifecycle.install(package(key, name="future", migrations={}, core_compatibility={"min": "0.0.9.0"}))
    lifecycle.deactivate("reports")
    assert observed["state_during_drain"] == "INACTIVE" and observed["cancelled"] == ["reports"]
    lifecycle.deactivate("notes")
    assert lifecycle.record("notes")["state"] == "INACTIVE"


def test_module_roles_are_confined_to_their_own_schema(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    for name in ("notes", "other"):
        lifecycle.install(package(key, name=name))
        lifecycle.activate(name)
    with psycopg.connect(module_postgres, autocommit=True, client_encoding="UTF8") as connection:
        connection.execute("CREATE TABLE public.platform_state (secret text)")
        connection.execute("INSERT INTO public.platform_state VALUES ('platform')")
        connection.execute("INSERT INTO mod_other.items (body) VALUES ('other-data')")
    url = module_url(module_postgres, lifecycle.credentials["notes"])
    for statement in ("SELECT * FROM mod_other.items", "SELECT * FROM public.platform_state",
                      "CREATE TABLE public.injected (x int)", "CREATE SCHEMA rogue"):
        with psycopg.connect(url, client_encoding="UTF8") as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)


def test_lab_modules_get_no_production_schema(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key, name="lab", placement={"target": "isolated_lab"}))
    with pytest.raises(LifecycleError, match="#491"):
        lifecycle.activate("lab")
    assert lifecycle.record("lab")["state"] == "INSTALLED"
    assert not rows(module_postgres, f"SELECT 1 FROM pg_namespace WHERE nspname='{schema_name('lab')}'")


def test_permission_approval_gates_activation(tmp_path, key, module_postgres):
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key, permissions=["notes.read"]))
    lifecycle.registry.report_node("n", [{**package(key, permissions=["notes.read"])["manifest"], "ready": True,
                                          "endpoint": "http://127.0.0.1:9"}])
    with pytest.raises(LifecycleError, match="approved"):
        lifecycle.activate("notes")
    lifecycle.registry.approve_permissions("notes", ["notes.read"])
    lifecycle.activate("notes")
    with pytest.raises(LifecycleError, match="approval"):
        lifecycle.update(package(key, version="1.1.0", permissions=["notes.read", "notes.write"]), confirmed=True)
    assert lifecycle.record("notes")["version"] == "1.0.0"


def test_approved_permissions_survive_restart_update_and_rollback(tmp_path, key, module_postgres):
    """#522 AC2: approvals live in the placement store, outside the module snapshot; after a
    CORE restart, an approved update and a rollback the module activates without a new approval."""
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key, permissions=["notes.read"]))
    lifecycle.registry.approve_permissions("notes", ["notes.read"])
    lifecycle.activate("notes")
    lifecycle.update(package(key, version="1.1.0", migrations={"m1": M1, "m2": M2},
                             permissions=["notes.read"]), confirmed=True)

    restarted = make(tmp_path, key, module_postgres)  # a new CORE process over the same state
    assert restarted.registry.placement.approvals["notes"] == ["notes.read"]
    restarted.rollback("notes")
    assert restarted.record("notes")["version"] == "1.0.0"
    restarted.deactivate("notes")
    assert restarted.activate("notes")["state"] == "ACTIVE"  # no second approval needed
    assert restarted.registry.placement.approvals["notes"] == ["notes.read"]


def test_admin_api_requires_admin_and_confirmation(tmp_path, key, module_postgres, monkeypatch):
    import hashlib
    import hmac
    from fastapi.testclient import TestClient
    from core.app import app
    from core.security import _session_token, session_secret
    lifecycle = make(tmp_path, key, module_postgres)

    class Runtime:
        modules = lifecycle.registry
    Runtime.lifecycle = lifecycle
    monkeypatch.setenv("VERTEP_MODULE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: Runtime)
    client = TestClient(app)
    base = "/api/nodes/module-registry"
    assert client.post(f"{base}/lifecycle/install", json={"package": package(key)}).status_code == 200
    assert client.post(f"{base}/lifecycle/activate", json={"module_id": "notes"}).status_code == 200
    unconfirmed = client.post(f"{base}/lifecycle/update", json={"package": package(key, version="1.1.0")})
    assert unconfirmed.status_code == 409 and "confirmation" in unconfirmed.text
    assert client.post(f"{base}/lifecycle/uninstall", json={"module_id": "notes", "confirm": True}).status_code == 200
    names = [a["name"] for a in client.get(f"{base}/archives").json()["archives"]]
    assert len(names) == 1 and client.get(f"{base}/archives/{names[0]}").status_code == 200
    monkeypatch.setenv("USERS_JSON", json.dumps({"reader": {"password_hash": "x", "role": "moderator"}}))
    session = _session_token("reader", "moderator")
    secret = session_secret()
    csrf = hmac.new(secret.encode() if isinstance(secret, str) else secret, session.encode(), hashlib.sha256).hexdigest()
    moderator = TestClient(app, headers={"Authorization": ""})
    moderator.cookies.set("vertep_session", session)
    moderator.cookies.set("vertep_csrf", csrf)
    assert moderator.get(f"{base}/archives").status_code == 403
    assert moderator.get(f"{base}/archives/{names[0]}").status_code == 403
    assert moderator.post(f"{base}/lifecycle/install", json={"package": package(key)}, headers={"x-csrf-token": csrf}).status_code == 403
    from core.module.gateway import read_audit
    actions = {(e["action"], e.get("outcome")) for e in read_audit()}
    assert ("module.lifecycle", "allowed") in actions and ("module.archive.download", "allowed") in actions


def test_module_ids_sharing_a_schema_or_role_never_share_data(tmp_path, key, module_postgres):
    """#493 AC2/AC3/AC7, #494: ``a-b``/``a_b`` and long IDs with one truncated
    prefix map onto one schema/role; the second module is rejected before
    provisioning and the first module's data stays intact."""
    from core.module.data import ModuleDataConflict
    lifecycle = make(tmp_path, key, module_postgres)
    long_id = "x" * 60
    for module_id in ("a-b", long_id):
        lifecycle.install(package(key, name=module_id))
        lifecycle.activate(module_id)
    with psycopg.connect(module_postgres, autocommit=True, client_encoding="UTF8") as connection:
        connection.execute("INSERT INTO mod_a_b.items (body) VALUES ('a-b data')")
    for other in ("a_b", "A-B", "a.b", long_id + "y"):
        with pytest.raises(LifecycleError, match="conflicts"):
            lifecycle.install(package(key, name=other))
        assert lifecycle.record(other) is None
    # Defence in depth below the lifecycle: the database itself refuses a
    # foreign module on a marked schema/role, for provisioning and removal.
    database = lifecycle.database
    for operation in (lambda: database.ensure("a_b"), lambda: database.drop("a_b"),
                      lambda: database.restore("a_b", {"migrations": [], "tables": {}}, {})):
        with pytest.raises(ModuleDataConflict):
            operation()
    assert rows(module_postgres, "SELECT body FROM mod_a_b.items") == [("a-b data",)]
    assert lifecycle.database.ensure("a-b")["password"] is None  # the owner keeps its account
    # A module named like the old archive-verification scratch schema keeps its
    # data when the other module is uninstalled.
    lifecycle.install(package(key, name="a-b__verify"))
    lifecycle.activate("a-b__verify")
    with psycopg.connect(module_postgres, autocommit=True, client_encoding="UTF8") as connection:
        connection.execute("INSERT INTO mod_a_b__verify.items (body) VALUES ('kept')")
    lifecycle.uninstall("a-b", confirmed=True)
    assert rows(module_postgres, "SELECT body FROM mod_a_b__verify.items") == [("kept",)]
    assert not rows(module_postgres, "SELECT 1 FROM pg_namespace WHERE nspname LIKE 'vtmp_verify_%'")
    # After the owner is gone the ID is free again, with a fresh schema.
    lifecycle.install(package(key, name="a_b"))
    lifecycle.activate("a_b")
    assert rows(module_postgres, "SELECT count(*) FROM mod_a_b.items") == [(0,)]


def test_uninstall_withholds_a_stale_instance_until_reinstalled(tmp_path, key, module_postgres):
    """#493 AC3/AC4, #494: a node still reporting a READY instance after uninstall
    gets no API/UI/dispatch access, also after a CORE restart."""
    lifecycle = make(tmp_path, key, module_postgres)
    lifecycle.install(package(key))
    report = lambda registry: registry.report_node("n", [{**package(key)["manifest"], "ready": True,
                                                          "endpoint": "http://127.0.0.1:9"}])
    report(lifecycle.registry)
    lifecycle.registry.set_placement("notes", ["n"])
    lifecycle.activate("notes")
    assert len(lifecycle.registry.ready_instances("notes")) == 1
    lifecycle.uninstall("notes", confirmed=True)
    assert lifecycle.registry.ready_instances("notes") == []
    assert "lifecycle_uninstalled" in lifecycle.registry.instances("notes")[0].reasons
    report(lifecycle.registry)  # the old instance reports again
    assert lifecycle.registry.ready_instances("notes") == []
    from core.module.gateway import _ready_instance  # API, secrets, module tokens and UI assets
    assert _ready_instance(lifecycle.registry, "notes") is None
    restarted = make(tmp_path, key, module_postgres)  # same lifecycle root and placement, new CORE process state
    report(restarted.registry)
    assert restarted.registry.ready_instances("notes") == []
    assert "lifecycle_uninstalled" in restarted.registry.instances("notes")[0].reasons
    restarted.install(package(key))
    restarted.activate("notes")
    assert len(restarted.registry.ready_instances("notes")) == 1


def test_a_refused_activation_says_why_the_self_test_failed(tmp_path, key, module_postgres):
    """#442 D17: the stand only saw «Module self-test failed» while no Worker was online."""
    def self_test(manifest):
        return False
    self_test.reason = "no online node reported this module (check the Worker and placement)"
    lifecycle = make(tmp_path, key, module_postgres, self_test=self_test)
    lifecycle.install(package(key))
    with pytest.raises(LifecycleError, match="Module self-test failed: no online node reported this module"):
        lifecycle.activate("notes")
