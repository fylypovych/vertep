"""CLI команди для backup/restore: vertep backup create|restore|list|verify."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .snapshot import Snapshot
from .storage import SnapshotStorage
from .resume import ResumeManager


def _storage() -> SnapshotStorage:
    return SnapshotStorage()


def cmd_create(args: argparse.Namespace) -> int:
    storage = _storage()
    version = getattr(args, "version", "0.0.0.0") or "0.0.0.0"
    incremental = bool(getattr(args, "incremental", False))
    parent = getattr(args, "parent", None)
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version=version,
        incremental=incremental, parent_snapshot_id=parent,
    )
    sid = storage.save_snapshot(snapshot)
    print(f"Snapshot created: {sid}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = ResumeManager(storage)
    snapshot_id = args.snapshot_id
    if not manager.verify_snapshot(snapshot_id):
        print(f"Snapshot {snapshot_id} failed integrity check", file=sys.stderr)
        return 1
    jobs = manager.restore_jobs(snapshot_id)
    print(f"Restored {len(jobs)} jobs from snapshot {snapshot_id}")
    for job in jobs:
        print(f"  - {job.get('job_id', 'unknown')}: {job.get('status', 'unknown')}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    storage = _storage()
    snapshots = storage.list_snapshots()
    if not snapshots:
        print("No snapshots found.")
        return 0
    for snap in snapshots:
        print(f"{snap['snapshot_id']}  {snap['created_at']}  "
              f"v{snap['version']}  jobs={snap['jobs_count']}  tasks={snap['tasks_count']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = ResumeManager(storage)
    ok = manager.verify_snapshot(args.snapshot_id)
    if ok:
        print(f"Snapshot {args.snapshot_id} is valid")
        return 0
    print(f"Snapshot {args.snapshot_id} is INVALID", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vertep backup", description="Backup/restore CORE state")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Створити знімок стану")
    p_create.add_argument("--version", default="0.0.0.0", help="Версія знімку")
    p_create.add_argument("--incremental", action="store_true", help="Інкрементальний знімок")
    p_create.add_argument("--parent", help="ІD батьківського знімка для інкрементальної копії")
    p_create.set_defaults(func=cmd_create)

    p_restore = sub.add_parser("restore", help="Відновити jobs зі знімка")
    p_restore.add_argument("snapshot_id", help="ІD знімка")
    p_restore.set_defaults(func=cmd_restore)

    p_list = sub.add_parser("list", help="Список знімків")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="Перевірити цілісність знімка")
    p_verify.add_argument("snapshot_id", help="ІD знімка")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
