"""Transactional module lifecycle with data, journal, rollback and archive.

install -> activate -> deactivate -> update/rollback -> uninstall for one
module. Layout under ``root``::

    state/<module>.json       current record (package, version, state, previous)
    journal/<module>.json     in-flight operation: op, phase, snapshot
    snapshots/<module>/<op>/  pre-change DB export + config + files + record
    data/<module>/config      module configuration (explicit ownership)
    data/<module>/files       module persistent files
    archives/                 admin-only (0700) uninstall archives (0600 tar)

Every change writes the journal *before* touching data. A failure, or a
restart that finds an unfinished journal (``recover``), restores the snapshot
(DB + config + files + record). A module is ACTIVE only after its self-test
passes; there is no READY without it. Archives are never deleted by Vertep.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .manifest import ModuleManifest
from .registry import ModuleRegistry

ARCHIVE_FORMAT = "vertep-module-archive/1"
JOURNAL_FORMAT = "vertep-module-journal/1"
INSTALLED, ACTIVE, INACTIVE, FAILED = "INSTALLED", "ACTIVE", "INACTIVE", "FAILED"
# Execution modules shipped inside the signed Vertep release (#497): their code
# and manifest come with the release itself, so no separate package signature.
BUNDLED = ("text", "image", "audio", "publisher", "backup", "characters", "brands", "storyboard", "publishing", "monitoring", "web-ui")


def bundled_manifest(name: str) -> dict:
    import yaml
    if name not in BUNDLED:
        raise LifecycleError(f"Unknown bundled module: {name}")
    path = Path(__file__).resolve().parents[2] / "modules" / name / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "module" in data:
        return data["module"]
    return data.get("manifest", data)


class LifecycleError(RuntimeError):
    pass


def _write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)
    # Durable before the next phase begins.
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def migration_sources(package: dict, manifest: ModuleManifest) -> dict[str, str]:
    import base64
    sources = {}
    for migration in manifest.migrations:
        name = f"migrations/{migration['id']}.sql"
        if name not in package["files"]:
            raise LifecycleError(f"Signed package lacks migration {name}")
        sources[migration["id"]] = base64.b64decode(package["files"][name]).decode("utf-8")
    return sources


class ModuleLifecycle:
    def __init__(self, root: Path, verify: Callable[[dict], ModuleManifest], *, database=None, registry=None,
                 self_test: Callable[[ModuleManifest], bool] = lambda manifest: True,
                 active_tasks: Callable[[str], list] = lambda module_id: [],
                 cancel_tasks: Callable[[str], None] = lambda module_id: None,
                 store_credentials: Callable[[str, dict], None] = lambda module_id, credentials: None,
                 system_versions: dict | None = None, drain_timeout: float = 30.0,
                 fault: Callable[[str], None] = lambda point: None) -> None:
        self.root = Path(root)
        self.verify = verify
        self.database = database
        self.registry = registry
        self.self_test = self_test
        self.active_tasks = active_tasks
        self.cancel_tasks = cancel_tasks
        self.store_credentials = store_credentials
        self.system_versions = system_versions or {}
        self.drain_timeout = drain_timeout
        self.fault = fault  # test hook: raises/exits at a named point
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        archives = self.root / "archives"
        archives.mkdir(parents=True, exist_ok=True)
        os.chmod(archives, 0o700)
        for record in self.records().values():
            self._publish(record)

    # -- records -------------------------------------------------------------------
    def _lock(self, module_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(module_id, threading.RLock())

    def _state_path(self, module_id: str) -> Path:
        return self.root / "state" / f"{module_id}.json"

    def _journal_path(self, module_id: str) -> Path:
        return self.root / "journal" / f"{module_id}.json"

    def data_dir(self, module_id: str) -> Path:
        return self.root / "data" / module_id

    def record(self, module_id: str) -> dict | None:
        path = self._state_path(module_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def records(self) -> dict[str, dict]:
        folder = self.root / "state"
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))} if folder.exists() else {}

    def _save(self, record: dict) -> None:
        _write_json(self._state_path(record["module_id"]), record)
        self._publish(record)

    def _publish(self, record: dict) -> None:
        """Dispatch/API/UI follow the lifecycle: only ACTIVE instances may be READY."""
        if self.registry is not None:
            self.registry.set_lifecycle_state(record["module_id"], record["state"], self._manifest(record))

    def _manifest(self, record: dict) -> ModuleManifest:
        return ModuleManifest.from_dict(record["package"]["manifest"])

    def _journal(self, module_id: str, **entry) -> None:
        _write_json(self._journal_path(module_id), {"format": JOURNAL_FORMAT, "module_id": module_id, **entry})

    # -- dependency guards -------------------------------------------------------------
    def _active_registry(self, exclude: str | None = None, extra: ModuleManifest | None = None) -> ModuleRegistry:
        registry = ModuleRegistry(self.system_versions)
        pending = {m.module_id: m for m in (self._manifest(r) for r in self.records().values()
                                             if r["state"] == ACTIVE and r["module_id"] != exclude)}
        if extra is not None:
            pending[extra.module_id] = extra
        while pending:
            loaded = [key for key, manifest in pending.items() if registry.register_module(manifest, force=True)]
            if not loaded:
                raise LifecycleError("Module dependencies, interfaces or CORE compatibility are not satisfied")
            for key in loaded:
                pending.pop(key)
        return registry

    def _dependents(self, module_id: str) -> list[str]:
        result = []
        record = self.record(module_id)
        provided = {(p["type"], p["name"]) for p in self._manifest(record).provides} if record else set()
        for other in self.records().values():
            if other["module_id"] == module_id or other["state"] != ACTIVE:
                continue
            manifest = self._manifest(other)
            names = {d["name"] if isinstance(d, dict) else d for d in manifest.dependencies}
            if module_id in names or any((r["type"], r["name"]) in provided for r in manifest.requires):
                result.append(other["module_id"])
        return sorted(result)

    # -- snapshots ----------------------------------------------------------------------
    def _snapshot(self, module_id: str, operation: str) -> Path:
        folder = self.root / "snapshots" / module_id / operation
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        record = self.record(module_id)
        exported = None
        if self.database is not None and self.database.exists(module_id):
            exported = self.database.export(module_id)
        _write_json(folder / "snapshot.json", {"record": record, "database": exported})
        source = self.data_dir(module_id)
        if source.exists():
            shutil.copytree(source, folder / "data", symlinks=True)
        return folder

    def _restore_snapshot(self, module_id: str, folder: Path) -> dict | None:
        snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
        record = snapshot["record"]
        if self.database is not None:
            if snapshot["database"] is not None:
                sources = migration_sources(record["package"], self._manifest(record))
                self.database.restore(module_id, snapshot["database"], sources)
            elif self.database.exists(module_id):
                self.database.drop(module_id)
        target = self.data_dir(module_id)
        if target.exists():
            shutil.rmtree(target)
        if (folder / "data").exists():
            shutil.copytree(folder / "data", target, symlinks=True)
        return record

    # -- operations ---------------------------------------------------------------------
    def _verify(self, package: dict) -> ModuleManifest:
        if "bundled" in package:
            # Always the manifest of the running release, never a supplied copy.
            return ModuleManifest.from_dict(bundled_manifest(package["bundled"]))
        return self.verify(package)  # signature, trust, provenance, UI digests

    def install_bundled(self, name: str) -> dict:
        return self.install({"bundled": name, "manifest": bundled_manifest(name), "files": {}})

    def install(self, package: dict) -> dict:
        manifest = self._verify(package)
        if "bundled" in package:
            package = {"bundled": package["bundled"], "manifest": bundled_manifest(package["bundled"]), "files": {}}
        with self._lock(manifest.module_id):
            if self.record(manifest.module_id):
                raise LifecycleError("Module is already installed; use update")
            migration_sources(package, manifest)
            self._active_registry(extra=manifest)  # dependencies + CORE compatibility before any change
            record = {"module_id": manifest.module_id, "version": manifest.version, "state": INSTALLED,
                      "package": package, "previous": None}
            self._save(record)
            return {"module_id": manifest.module_id, "state": INSTALLED}

    def _prepare_data(self, record: dict) -> None:
        manifest = self._manifest(record)
        for folder in ("config", "files"):
            (self.data_dir(manifest.module_id) / folder).mkdir(parents=True, exist_ok=True)
        needs_database = manifest.migrations and manifest.placement["target"] != "isolated_lab"
        if manifest.migrations and manifest.placement["target"] == "isolated_lab":
            raise LifecycleError("LAB state uses its own storage (#491); no production database schema")
        if needs_database and self.database is None:
            raise LifecycleError("Module database is not configured")
        if needs_database:
            credentials = self.database.ensure(manifest.module_id)
            if credentials.get("password"):
                self.store_credentials(manifest.module_id, credentials)
            self.fault("before_migrations")
            self.database.migrate(manifest.module_id, manifest.migrations, migration_sources(record["package"], manifest))
            self.fault("after_migrations")

    def activate(self, module_id: str) -> dict:
        with self._lock(module_id):
            record = self.record(module_id)
            if record is None:
                raise LifecycleError("Module is not installed")
            if record["state"] == ACTIVE:
                return {"module_id": module_id, "state": ACTIVE}
            manifest = self._manifest(record)
            self._active_registry(exclude=module_id, extra=manifest)
            if self.registry is not None:
                approved = set(self.registry.placement.approvals.get(module_id, []))
                if not set(manifest.permissions) <= approved:
                    raise LifecycleError("Module permissions are not approved by the administrator")
            operation = uuid.uuid4().hex
            snapshot = self._snapshot(module_id, operation)
            self._journal(module_id, op="activate", phase="snapshot_taken", snapshot=str(snapshot), at=time.time())
            try:
                self._prepare_data(record)
                self._journal(module_id, op="activate", phase="applied", snapshot=str(snapshot), at=time.time())
                if not self.self_test(manifest):
                    raise LifecycleError("Module self-test failed")
                record["state"] = ACTIVE
                self._save(record)
            except BaseException:
                self._rollback_to(module_id, snapshot)
                self._journal_path(module_id).unlink(missing_ok=True)  # undone: nothing left to recover
                raise
            self._journal_path(module_id).unlink()
            return {"module_id": module_id, "state": ACTIVE}

    def deactivate(self, module_id: str, *, force: bool = False) -> dict:
        with self._lock(module_id):
            record = self.record(module_id)
            if record is None:
                raise LifecycleError("Module is not installed")
            dependents = self._dependents(module_id)
            if dependents and not force:
                raise LifecycleError(f"Active modules depend on it: {', '.join(dependents)}")
            self._drain(record)
            return {"module_id": module_id, "state": record["state"]}

    def _drain(self, record: dict) -> None:
        """Stop new dispatch first, then let active tasks finish or cancel them."""
        if record["state"] == ACTIVE:
            record["state"] = INACTIVE
            self._save(record)
        deadline = time.monotonic() + self.drain_timeout
        while self.active_tasks(record["module_id"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.active_tasks(record["module_id"]):
            self.cancel_tasks(record["module_id"])

    def update(self, package: dict, *, confirmed: bool) -> dict:
        if confirmed is not True:
            raise LifecycleError("Update requires explicit administrator confirmation")
        if "bundled" in package:
            raise LifecycleError("Bundled modules are updated with the Vertep release")
        manifest = self.verify(package)
        module_id = manifest.module_id
        with self._lock(module_id):
            record = self.record(module_id)
            if record is None:
                raise LifecycleError("Module is not installed")
            if record["version"] == manifest.version:
                raise LifecycleError("Module version is already installed")
            migration_sources(package, manifest)
            was_active = record["state"] == ACTIVE
            # New version must satisfy its own dependencies and every active dependent.
            self._active_registry(exclude=module_id, extra=manifest)
            if self.registry is not None and not set(manifest.permissions) <= set(self.registry.placement.approvals.get(module_id, [])):
                raise LifecycleError("Expanded permissions need administrator approval before update")
            operation = uuid.uuid4().hex
            self._drain(record)
            snapshot = self._snapshot(module_id, operation)
            self._journal(module_id, op="update", phase="snapshot_taken", snapshot=str(snapshot),
                          from_version=record["version"], to_version=manifest.version, was_active=was_active, at=time.time())
            previous = {key: record[key] for key in ("version", "package")}
            try:
                updated = {"module_id": module_id, "version": manifest.version, "state": INACTIVE,
                           "package": package, "previous": {**previous, "snapshot": str(snapshot)}}
                self._save(updated)
                self.fault("after_record")
                self._prepare_data(updated)
                self._journal(module_id, op="update", phase="applied", snapshot=str(snapshot),
                              from_version=record["version"], to_version=manifest.version, was_active=was_active, at=time.time())
                self.fault("after_apply")
                if not self.self_test(manifest):
                    raise LifecycleError("Updated module self-test failed")
                updated["state"] = ACTIVE if was_active else INACTIVE
                self._save(updated)
            except BaseException:
                self._rollback_to(module_id, snapshot, reactivate=was_active)
                self._journal_path(module_id).unlink(missing_ok=True)  # undone: nothing left to recover
                raise
            self._journal_path(module_id).unlink()
            return {"module_id": module_id, "state": updated["state"], "version": manifest.version}

    def rollback(self, module_id: str) -> dict:
        """Return to the version before the last update, with its data snapshot."""
        with self._lock(module_id):
            record = self.record(module_id)
            if not record or not record.get("previous"):
                raise LifecycleError("No previous version to roll back to")
            was_active = record["state"] == ACTIVE
            self._drain(record)
            self._journal(module_id, op="rollback", phase="snapshot_taken", snapshot=record["previous"]["snapshot"], at=time.time())
            self._rollback_to(module_id, Path(record["previous"]["snapshot"]), reactivate=was_active)
            self._journal_path(module_id).unlink(missing_ok=True)
            return {"module_id": module_id, "state": self.record(module_id)["state"], "version": self.record(module_id)["version"]}

    def _rollback_to(self, module_id: str, snapshot: Path, reactivate: bool = False) -> None:
        record = self._restore_snapshot(module_id, snapshot)
        if record is None:
            self._state_path(module_id).unlink(missing_ok=True)
            if self.registry is not None:
                self.registry.set_lifecycle_state(module_id, None)
            return
        state = record["state"]
        if reactivate or state == ACTIVE:
            # Publish the restored version first (inactive) so nodes converge to it,
            # then require its own self-test: no false READY.
            self._save({**record, "state": INACTIVE})
            state = ACTIVE if self.self_test(self._manifest(record)) else FAILED
        self._save({**record, "state": state})

    def recover(self) -> list[dict]:
        """After a restart: finish or undo every unfinished operation."""
        results = []
        folder = self.root / "journal"
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            journal = json.loads(path.read_text(encoding="utf-8"))
            module_id = journal["module_id"]
            with self._lock(module_id):
                if journal["op"] == "uninstall":
                    if journal["phase"] == "archive_verified":
                        self._remove_data(module_id)
                        outcome = "uninstall_completed"
                    else:
                        record = self.record(module_id)
                        if record:
                            self._save({**record, "state": INACTIVE})
                        outcome = "uninstall_aborted"
                else:
                    self._rollback_to(module_id, Path(journal["snapshot"]), reactivate=bool(journal.get("was_active")))
                    outcome = "rolled_back"
                path.unlink()
                results.append({"module_id": module_id, "op": journal["op"], "outcome": outcome})
        return results

    # -- uninstall + archive ---------------------------------------------------------------
    def uninstall(self, module_id: str, *, confirmed: bool) -> dict:
        if confirmed is not True:
            raise LifecycleError("Uninstall requires explicit administrator confirmation")
        with self._lock(module_id):
            record = self.record(module_id)
            if record is None:
                raise LifecycleError("Module is not installed")
            dependents = self._dependents(module_id)
            if dependents:
                raise LifecycleError(f"Active modules depend on it: {', '.join(dependents)}")
            self._drain(record)
            self._journal(module_id, op="uninstall", phase="archiving", at=time.time())
            try:
                archive = self.create_archive(module_id)
                self.verify_archive(archive)
            except BaseException as error:
                # Archive failure blocks deletion; the module stays installed and inactive.
                self._journal_path(module_id).unlink(missing_ok=True)
                raise LifecycleError(f"Uninstall blocked: archive failed ({error})") from error
            self._journal(module_id, op="uninstall", phase="archive_verified", archive=str(archive), at=time.time())
            self.fault("after_archive")
            self._remove_data(module_id)
            self._journal_path(module_id).unlink()
            return {"module_id": module_id, "state": "UNINSTALLED", "archive": archive.name}

    def _remove_data(self, module_id: str) -> None:
        if self.database is not None and self.database.exists(module_id):
            self.database.drop(module_id)
        shutil.rmtree(self.data_dir(module_id), ignore_errors=True)
        shutil.rmtree(self.root / "snapshots" / module_id, ignore_errors=True)
        self._state_path(module_id).unlink(missing_ok=True)
        if self.registry is not None:
            self.registry.set_lifecycle_state(module_id, None)

    def create_archive(self, module_id: str) -> Path:
        """Unencrypted, admin-only tar: DB export, migrations, config, files, signed package."""
        record = self.record(module_id)
        manifest = self._manifest(record)
        members: dict[str, bytes] = {
            "manifest.json": json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True).encode(),
            "package.json": json.dumps(record["package"], ensure_ascii=False, sort_keys=True).encode(),
        }
        if self.database is not None and self.database.exists(module_id):
            members["database.json"] = json.dumps(self.database.export(module_id), ensure_ascii=False, sort_keys=True).encode()
        for migration_id, source in migration_sources(record["package"], manifest).items():
            members[f"migrations/{migration_id}.sql"] = source.encode()
        data = self.data_dir(module_id)
        for kind in ("config", "files"):
            base = data / kind
            for path in sorted(base.rglob("*")) if base.exists() else []:
                if path.is_file() and not path.is_symlink():
                    members[f"{kind}/{path.relative_to(base).as_posix()}"] = path.read_bytes()
        index = {"format": ARCHIVE_FORMAT, "module_id": module_id, "version": record["version"],
                 "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "encrypted": False,
                 "checksums": {name: _sha256(content) for name, content in members.items()}}
        target = self.root / "archives" / f"{module_id}-{record['version']}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}.tar"
        temporary = target.with_suffix(".partial")
        with tarfile.open(temporary, "w") as tar:
            for name, content in {"archive.json": json.dumps(index, sort_keys=True).encode(), **members}.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(content), 0o600, int(time.time())
                tar.addfile(info, io.BytesIO(content))
        os.chmod(temporary, 0o600)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(target)
        return target

    @staticmethod
    def read_archive(path: Path) -> tuple[dict, dict[str, bytes]]:
        members = {}
        with tarfile.open(path, "r") as tar:
            for info in tar.getmembers():
                if not info.isfile() or info.name.startswith("/") or ".." in info.name.split("/"):
                    raise LifecycleError("Unsafe archive member")
                members[info.name] = tar.extractfile(info).read()
        index = json.loads(members.pop("archive.json"))
        if index.get("format") != ARCHIVE_FORMAT:
            raise LifecycleError("Unsupported module archive format")
        if set(members) != set(index["checksums"]) or any(_sha256(members[n]) != d for n, d in index["checksums"].items()):
            raise LifecycleError("Module archive integrity check failed")
        return index, members

    def verify_archive(self, path: Path) -> None:
        """Integrity + a real restore of the DB export into a scratch schema compared with live data."""
        index, members = self.read_archive(path)
        if "database.json" in members and self.database is not None:
            from .data import schema_name
            exported = json.loads(members["database.json"])
            sources = {name[len("migrations/"):-4]: content.decode() for name, content in members.items() if name.startswith("migrations/")}
            scratch = schema_name(index["module_id"]) + "__verify"
            try:
                self.database.restore(index["module_id"], exported, sources, schema=scratch)
                if self.database.fingerprint(index["module_id"], scratch) != self.database.fingerprint(index["module_id"]):
                    raise LifecycleError("Restored archive data differs from live data")
            finally:
                self.database.drop_schema(scratch)

    def restore_archive(self, path: Path) -> dict:
        """Reinstall a module from its archive (state INSTALLED until the admin activates)."""
        index, members = self.read_archive(path)
        package = json.loads(members["package.json"])
        manifest = self._verify(package)
        module_id = manifest.module_id
        with self._lock(module_id):
            if self.record(module_id):
                raise LifecycleError("Module is installed; uninstall before restoring an archive")
            if "database.json" in members and self.database is not None:
                credentials = self.database.ensure(module_id)
                if credentials.get("password"):
                    self.store_credentials(module_id, credentials)
                self.database.restore(module_id, json.loads(members["database.json"]), migration_sources(package, manifest))
            data = self.data_dir(module_id)
            for name, content in members.items():
                kind = name.split("/", 1)[0]
                if kind in ("config", "files"):
                    target = data / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            record = {"module_id": module_id, "version": manifest.version, "state": INSTALLED, "package": package, "previous": None}
            self._save(record)
            return {"module_id": module_id, "state": INSTALLED, "version": manifest.version}

    def archives(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "archives").glob("*.tar")):
            result.append({"name": path.name, "size": path.stat().st_size})
        return result
