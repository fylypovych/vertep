"""CLI команди для backup/restore: vertep backup create|restore|list|verify."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .resume import ResumeManager, RestoreState
from .snapshot import Snapshot
from .storage import SnapshotStorage


def _storage() -> SnapshotStorage:
    return SnapshotStorage()


def _state(storage: SnapshotStorage) -> RestoreState:
    return RestoreState(storage)


def _manager(storage: SnapshotStorage) -> ResumeManager:
    return ResumeManager(storage)


def cmd_create(args: argparse.Namespace) -> int:
    storage = _storage()
    version = getattr(args, "version", "0.0.0.0") or "0.0.0.0"
    incremental = bool(getattr(args, "incremental", False))
    parent = getattr(args, "parent", None)
    snapshot = Snapshot.create(
        jobs=[], tasks=[], artifacts=[], settings={}, version=version,
        incremental=incremental, parent_snapshot_id=parent,
    )
    from .workflow import capture
    capture(snapshot)
    if incremental:
        if not parent or not storage.verify_chain(parent):
            raise ValueError("Incremental snapshot requires a verified parent")
    sid = storage.save_snapshot(snapshot)
    print(f"Snapshot created: {sid}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    snapshot_id = args.snapshot_id
    if not manager.verify_snapshot(snapshot_id):
        print(f"Snapshot {snapshot_id} failed integrity check", file=sys.stderr)
        return 1
    jobs = manager.restore_jobs(snapshot_id)
    from .workflow import restore
    restored_workflows = restore(storage.load_snapshot(snapshot_id))
    print(f"Restored {restored_workflows} workflows")
    print(f"Restored {len(jobs)} jobs from snapshot {snapshot_id}")
    for job in jobs:
        print(f"  - {job.get('job_id', 'unknown')}: {job.get('status', 'unknown')}")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    storage = _storage()
    state = _state(storage)
    if state.consume_confirmation(args.snapshot_id, args.token):
        print(f"Confirmation consumed for snapshot {args.snapshot_id}")
        return 0
    print(f"Invalid or already used confirmation token", file=sys.stderr)
    return 1


def cmd_progress(args: argparse.Namespace) -> int:
    storage = _storage()
    state = _state(storage)
    operation = state.load_operation(args.snapshot_id)
    if operation is None:
        print(f"No restore operation for snapshot {args.snapshot_id}", file=sys.stderr)
        return 1
    print(json.dumps(operation, ensure_ascii=False, indent=2))
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    storage = _storage()
    state = _state(storage)
    guard = state.acquire_guard(args.snapshot_id)
    if guard is None:
        print(f"Guard already held for snapshot {args.snapshot_id}", file=sys.stderr)
        return 1
    print(f"Guard acquired: {guard}")
    state.release_guard(guard)
    print("Guard released")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    storage = _storage()
    snapshots = storage.list_snapshots()
    if not snapshots:
        print("No snapshots found.")
        return 0
    for snap in snapshots:
        print(f"{snap['snapshot_id']}  {snap['created_at']}  "
              f"v{snap['version']}  jobs={snap['jobs_count']}  tasks={snap['tasks_count']}  "
              f"modules={snap.get('modules_count', 0)}  workflows={snap.get('workflows_count', 0)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    ok = manager.verify_snapshot(args.snapshot_id)
    if ok:
        print(f"Snapshot {args.snapshot_id} is valid")
        return 0
    print(f"Snapshot {args.snapshot_id} is INVALID", file=sys.stderr)
    return 1


def cmd_module_save(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    if not manager.verify_snapshot(args.snapshot_id):
        print(f"Snapshot {args.snapshot_id} failed integrity check", file=sys.stderr)
        return 1
    state = json.loads(args.state)
    manager.save_module_state(args.snapshot_id, args.module, state)
    print(f"Module state saved for {args.module} in snapshot {args.snapshot_id}")
    return 0


def cmd_module_load(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    state = manager.restore_module_state(args.snapshot_id, args.module)
    if state is None:
        print(f"No module state for {args.module} in snapshot {args.snapshot_id}", file=sys.stderr)
        return 1
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_module_list(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    modules = manager.list_modules_with_state(args.snapshot_id)
    if not modules:
        print("No modules with state in snapshot.")
        return 0
    for name in modules:
        print(name)
    return 0


def cmd_workflow_save(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    if not manager.verify_snapshot(args.snapshot_id):
        print(f"Snapshot {args.snapshot_id} failed integrity check", file=sys.stderr)
        return 1
    checkpoint = json.loads(args.checkpoint)
    manager.save_workflow_checkpoint(args.snapshot_id, args.job_id, checkpoint)
    print(f"Workflow checkpoint saved for job {args.job_id} in snapshot {args.snapshot_id}")
    return 0


def cmd_workflow_load(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    checkpoint = manager.restore_workflow_checkpoint(args.snapshot_id, args.job_id)
    if checkpoint is None:
        print(f"No workflow checkpoint for job {args.job_id} in snapshot {args.snapshot_id}",
              file=sys.stderr)
        return 1
    print(json.dumps(checkpoint, ensure_ascii=False, indent=2))
    return 0


def cmd_workflow_list(args: argparse.Namespace) -> int:
    storage = _storage()
    manager = _manager(storage)
    jobs = manager.list_workflow_checkpoints(args.snapshot_id)
    if not jobs:
        print("No workflow checkpoints in snapshot.")
        return 0
    for job_id in jobs:
        print(job_id)
    return 0


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

    p_confirm = sub.add_parser("confirm", help="Спожити одноразовий токен підтвердження відновлення")
    p_confirm.add_argument("snapshot_id", help="ІD знімка")
    p_confirm.add_argument("token", help="Токен підтвердження")
    p_confirm.set_defaults(func=cmd_confirm)

    p_progress = sub.add_parser("progress", help="Показати стан операції відновлення")
    p_progress.add_argument("snapshot_id", help="ІD знімка")
    p_progress.set_defaults(func=cmd_progress)

    p_guard = sub.add_parser("guard", help="Отримати/повільнити гвард відновлення")
    p_guard.add_argument("snapshot_id", help="ІD знімка")
    p_guard.set_defaults(func=cmd_guard)

    p_list = sub.add_parser("list", help="Список знімків")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="Перевірити цілісність знімка")
    p_verify.add_argument("snapshot_id", help="ІD знімка")
    p_verify.set_defaults(func=cmd_verify)

    p_module_save = sub.add_parser("module-save", help="Зберегти стан модуля у знімок")
    p_module_save.add_argument("snapshot_id", help="ІD знімка")
    p_module_save.add_argument("module", help="Назва модуля")
    p_module_save.add_argument("state", help="JSON стан модуля")
    p_module_save.set_defaults(func=cmd_module_save)

    p_module_load = sub.add_parser("module-load", help="Завантажити стан модуля зі знімка")
    p_module_load.add_argument("snapshot_id", help="ІD знімка")
    p_module_load.add_argument("module", help="Назва модуля")
    p_module_load.set_defaults(func=cmd_module_load)

    p_module_list = sub.add_parser("module-list", help="Список модулів зі станом у знімку")
    p_module_list.add_argument("snapshot_id", help="ІD знімка")
    p_module_list.set_defaults(func=cmd_module_list)

    p_workflow_save = sub.add_parser("workflow-save", help="Зберегти чекпоінт Workflow Engine")
    p_workflow_save.add_argument("snapshot_id", help="ІD знімка")
    p_workflow_save.add_argument("job_id", help="ІD джоби")
    p_workflow_save.add_argument("checkpoint", help="JSON чекпоінту")
    p_workflow_save.set_defaults(func=cmd_workflow_save)

    p_workflow_load = sub.add_parser("workflow-load", help="Завантажити чекпоінт Workflow Engine")
    p_workflow_load.add_argument("snapshot_id", help="ІD знімка")
    p_workflow_load.add_argument("job_id", help="ІD джоби")
    p_workflow_load.set_defaults(func=cmd_workflow_load)

    p_workflow_list = sub.add_parser("workflow-list", help="Список чекпоінтів Workflow Engine")
    p_workflow_list.add_argument("snapshot_id", help="ІD знімка")
    p_workflow_list.set_defaults(func=cmd_workflow_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
