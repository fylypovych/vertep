#!/usr/bin/env python3
"""Component releases and change-impact CI (#510).

  impact   --base REF --head REF | --files a b c   -> JSON: full_suite, components, tests
  plan     --components a,b --sha SHA [--releases F] -> JSON: tag per component, collision checks,
                                                       image to build, backfill of packageless releases
  history                                           -> JSON: shared release -> first independent version
  manifest --component X --sha SHA --private-key K --output F [--artifact name=path ...] [--image digest]
  package  --component X --private-key K --output F [--image ref@sha256:...] [--key-id ID]
           [--extra-image service=ref@sha256:...]
                                                    -> signed module package (package.json + .sha256)
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import components as c  # noqa: E402


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def _tags() -> dict[str, str]:
    """tag -> commit, from the local repository (the workflow fetches all tags first)."""
    result = {}
    for line in _git("show-ref", "--tags", "-d").splitlines():
        sha, ref = line.split(" ", 1)
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            result[name[:-3]] = sha
        else:
            result.setdefault(name, sha)
    return result


def cmd_impact(args) -> dict:
    if args.files is not None:
        changed = args.files
    elif not args.base or set(args.base) == {"0"}:
        return {"full_suite": True, "components": [], "tests": [], "reasons": {"unknown": ["no base commit"]},
                "changed_count": 0, "ignored": []}
    else:
        try:
            changed = _git("diff", "--name-only", args.base, args.head).splitlines()
        except subprocess.CalledProcessError:
            return {"full_suite": True, "components": [], "tests": [], "reasons": {"unknown": ["base not available"]},
                    "changed_count": 0, "ignored": []}
    normalized = sorted({p.strip().lstrip("./") for p in changed if p.strip()})
    result = c.impact(normalized)
    return {**result.to_dict(), "changed_count": len(normalized), "ignored": result.ignored}


def cmd_plan(args) -> dict:
    registry = c.load_registry()
    tags = _tags()
    assets = {}
    if args.releases:
        # [{"tag": ..., "assets": [names]}] from the GitHub releases API.
        assets = {item["tag"]: set(item.get("assets") or []) for item in json.loads(Path(args.releases).read_text())}
    plan = {}
    for component in [item for item in args.components.split(",") if item]:
        spec = registry["components"][component]
        version = c.declared_version(component, ROOT, registry)
        entry = {"version": version, "kind": spec["kind"]}
        if spec.get("image"):
            entry["image"] = spec["image"]
        if spec.get("extra_images"):
            entry["extra_images"] = spec["extra_images"]
        try:
            entry["tag"] = c.check_release(component, version, args.sha, tags)
            entry["publish"] = spec["kind"] == "module" and entry["tag"] not in tags
        except c.ComponentError as error:
            entry["error"] = str(error)
            entry["publish"] = False
        entry["backfill"] = False
        tag = c.tag_name(component, version)
        entry["tag"] = tag  # also when the release already exists (backfill uploads to it)
        if tag in tags:
            changed = _git("diff", "--name-only", tags[tag], args.sha).split()
            if not [path for path in changed if any(fnmatch.fnmatch(path, pattern) for pattern in spec["paths"])]:
                # Same files as the published release: nothing to publish and nothing refused
                # (a shared change elsewhere only pulled the component into the run).
                entry.pop("error", None)
                entry["publish"] = False
                if (spec["kind"] == "module" and spec.get("image") and tag in assets
                        and "package.json" not in assets[tag]):
                    # Published before packages existed (#510): add its package, no new tag.
                    entry.update(backfill=True, commit=tags[tag])
        plan[component] = entry
    return plan


def cmd_history(_args) -> dict:
    log = []
    raw = _git("log", "--reverse", "--format=@@%H%x09%s", "--name-only")
    for block in raw.split("@@")[1:]:
        head, *files = block.strip().splitlines()
        sha, subject = head.split("\t", 1)
        log.append((sha, subject, [f for f in files if f]))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = c.shared_releases(log, changelog)
    return {"shared_releases": len(releases), "last_shared": releases[-1][0] if releases else None,
            "modules": c.history_mapping(releases, list(_tags()))}


def cmd_manifest(args) -> dict:
    import yaml
    registry = c.load_registry()
    spec = registry["components"][args.component]
    version = c.declared_version(args.component, ROOT, registry)
    artifacts = []
    for item in args.artifact or []:
        name, path = item.split("=", 1)
        data = Path(path).read_bytes()
        artifacts.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    for digest in args.image or []:
        artifacts.append({"name": "image", "digest": digest})
    # The released package's manifest carries compatibility and API (#510), as in cmd_package.
    source = spec.get("package_manifest") or spec["version_source"]
    manifest = yaml.safe_load((ROOT / source).read_text(encoding="utf-8")) if spec["kind"] == "module" else {}
    module = manifest.get("module") if isinstance(manifest.get("module"), dict) else manifest
    compatibility = {"core": module.get("core_compatibility")
                     or {"min": module.get("min_core_version"), "max": module.get("max_core_version") or None},
                     "api": module.get("api") and {"namespace": module["api"].get("namespace"),
                                                   "version": module["api"].get("version")}}
    dependencies = [{"component": item} if isinstance(item, str) else item
                    for item in (module.get("dependencies") or [])]
    document = c.build_manifest(args.component, version, args.sha, artifacts,
                                compatibility=compatibility, dependencies=dependencies, registry=registry)
    signed = c.sign_manifest(document, Path(args.private_key).read_bytes())
    Path(args.output).write_text(json.dumps(signed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"written": args.output, "tag": signed["tag"]}


def cmd_package(args) -> dict:
    key_id = args.key_id
    trust = json.loads(c.PUBLISHER_TRUST.read_text(encoding="utf-8")) if c.PUBLISHER_TRUST.exists() else {}
    keys = (trust.get("vertep") or {}).get("keys") or {}
    if key_id is None:
        if not keys:
            raise SystemExit("config/module-trust/publishers.json has no vertep key: run scripts/create-module-signing-key.py")
        key_id = list(keys)[-1]
    extra = dict(item.split("=", 1) for item in args.extra_image or [])
    package = c.sign_package(c.build_package(args.component, image=args.image, extra_images=extra),
                             Path(args.private_key).read_bytes(), key_id)
    if key_id in keys:
        # The signature must verify with the public key CORE trusts, before anything is published.
        from core.module.packages import verify_package
        from core.module.trust import TrustStore
        verify_package(package, TrustStore.from_root(c.PUBLISHER_TRUST.parent))
    data = (json.dumps(package, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    Path(args.output).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    Path(args.output + ".sha256").write_text(f"{digest}  package.json\n", encoding="utf-8")
    return {"written": args.output, "sha256": digest, "signer": key_id, "image": args.image}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    impact = sub.add_parser("impact")
    impact.add_argument("--base")
    impact.add_argument("--head", default="HEAD")
    impact.add_argument("--files", nargs="*")
    plan = sub.add_parser("plan")
    plan.add_argument("--components", required=True)
    plan.add_argument("--sha", required=True)
    plan.add_argument("--releases")
    sub.add_parser("history")
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--component", required=True)
    manifest.add_argument("--sha", required=True)
    manifest.add_argument("--private-key", required=True)
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--artifact", action="append")
    manifest.add_argument("--image", action="append")
    package = sub.add_parser("package")
    package.add_argument("--component", required=True)
    package.add_argument("--private-key", required=True)
    package.add_argument("--output", required=True)
    package.add_argument("--image")
    package.add_argument("--key-id")
    package.add_argument("--extra-image", action="append", help="service=ref@sha256:... (LAB lab-web)")
    args = parser.parse_args()
    handler = {"impact": cmd_impact, "plan": cmd_plan, "history": cmd_history, "manifest": cmd_manifest,
               "package": cmd_package}[args.command]
    print(json.dumps(handler(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
