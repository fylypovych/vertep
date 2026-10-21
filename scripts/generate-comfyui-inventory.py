#!/usr/bin/env python3
"""Generate the expected ComfyUI runtime inventory of a GPU node.

Issue #120/#178.1: the GPU node may only become ready when it can prove that its
runtime matches a pinned contract.  The contract has two halves:

* the **expected** inventory — the immutable component pins of the ComfyUI
  image plus the content of the workflows and models the node serves.  This
  script produces it from the same Dockerfile that builds the image, so a pin
  can never be recorded in one place and built from another;
* the **observed** report — what the image was actually built from.  The
  Dockerfile writes it at build time and the ComfyUI entrypoint publishes it
  onto the shared config volume, where the Worker reads it
  (``COMFYUI_ROOT``).

The generated document is written where the Worker expects it
(``config/comfyui-runtime-inventory.json``), which the deployment mounts into
the GPU node as ``COMFYUI_RUNTIME_INVENTORY``.

Usage::

    python scripts/generate-comfyui-inventory.py --root .
    python scripts/generate-comfyui-inventory.py --root . \\
        --workflows-root storage/workflows --models-root models
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.comfyui_runtime import (RuntimeInventoryError, build_expected_inventory,  # noqa: E402
                                      expected_inventory_from_dockerfile,
                                      write_expected_inventory)

DEFAULT_DOCKERFILE = "docker/comfyui-nvidia/Dockerfile"
DEFAULT_PINS = "config/comfyui-runtime-pins.json"
DEFAULT_OUTPUT = "config/comfyui-runtime-inventory.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Згенерувати очікуваний runtime inventory GPU-вузла")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="Корінь інсталяції Vertep")
    parser.add_argument("--dockerfile", type=Path, default=None,
                        help="Dockerfile ComfyUI-образу (за замовчуванням "
                             f"{DEFAULT_DOCKERFILE})")
    parser.add_argument("--pins", type=Path, default=None,
                        help="Документ із зафіксованими компонентами, який їде "
                             f"разом із runtime bundle (за замовчуванням {DEFAULT_PINS})")
    parser.add_argument("--workflows-root", type=Path, default=None,
                        help="Корінь workflow-файлів, які обслуговує вузол")
    parser.add_argument("--models-root", type=Path, default=None,
                        help="Корінь моделей, які обслуговує вузол")
    parser.add_argument("--output", type=Path, default=None,
                        help=f"Шлях очікуваного inventory (за замовчуванням {DEFAULT_OUTPUT})")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="Вивести inventory у stdout без запису файлу")
    args = parser.parse_args()

    root = args.root.resolve()
    output = (args.output or root / DEFAULT_OUTPUT).resolve()
    workflows_root = (args.workflows_root or root / "storage" / "workflows").resolve()
    models_root = (args.models_root or root / "models").resolve()

    try:
        if args.pins is not None:
            pins = json.loads(args.pins.read_text(encoding="utf-8"))
            inventory = build_expected_inventory(
                components=pins.get("components") or {},
                workflows_root=workflows_root, models_root=models_root)
        else:
            dockerfile = (args.dockerfile or root / DEFAULT_DOCKERFILE).resolve()
            if not dockerfile.is_file():
                print(f"Dockerfile is missing: {dockerfile}", file=sys.stderr)
                return 2
            inventory = expected_inventory_from_dockerfile(
                dockerfile.read_text(encoding="utf-8"),
                workflows_root=workflows_root, models_root=models_root)
    except RuntimeInventoryError as error:
        print(f"Runtime inventory rejected: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"Runtime inventory could not be read: {error}", file=sys.stderr)
        return 1

    if args.print_only:
        print(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    write_expected_inventory(output, inventory)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
