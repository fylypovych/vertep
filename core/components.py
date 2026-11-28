"""Component versions, signed component releases and change-impact CI (#510, VAR #509).

CORE and every official module are versioned independently. A component release is
a stable, signed manifest published under the tag ``<component>.<version>`` from
``fylypovych/vertep`` after the tests its change requires have passed.

Numbering (owner decision, #509):
* every component uses ``A.B.C.D``; after 99 the digit carries into the previous
  position (``0.0.1.99`` -> ``0.0.2.0``);
* CORE continues the shared Vertep sequence (``VERSION``);
* an existing module continues its history: the first independent version is the
  next free number after the last shared Vertep version that changed it;
* a new module without history starts at ``0.0.0.1``.
Historical shared tags (plain ``A.B.C.D``) are kept untouched.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "components.json"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")
TAG_RE = re.compile(r"^([a-z][a-z0-9-]*)\.(\d+\.\d+\.\d+\.\d+)$")
MANIFEST_FORMAT = 1
FIRST_VERSION = "0.0.0.1"


class ComponentError(ValueError):
    pass


# -- versions -------------------------------------------------------------------------

def parse_version(value: str) -> tuple[int, int, int, int]:
    match = VERSION_RE.fullmatch(str(value or ""))
    if not match:
        raise ComponentError(f"Version must be A.B.C.D: {value!r}")
    parts = tuple(int(part) for part in match.groups())
    if any(part > 99 for part in parts[1:]):
        raise ComponentError(f"Digits after the first carry over after 99: {value!r}")
    return parts  # type: ignore[return-value]


def format_version(parts: tuple[int, int, int, int]) -> str:
    return ".".join(str(part) for part in parts)


def next_version(value: str) -> str:
    """The next number in the sequence; 99 carries into the previous digit."""
    a, b, c, d = parse_version(value)
    d += 1
    if d > 99:
        d, c = 0, c + 1
    if c > 99:
        c, b = 0, b + 1
    if b > 99:
        b, a = 0, a + 1
    return format_version((a, b, c, d))


def version_key(component: str, value: str, registry: dict | None = None) -> tuple[int, int, int, int]:
    """Ordering key; legacy manifest values (e.g. ``1.0.0``) map to their recorded base."""
    registry = registry or load_registry()
    legacy = registry["components"].get(component, {}).get("legacy_versions", {})
    return parse_version(legacy.get(value, value))


# -- tags -------------------------------------------------------------------------------

def tag_name(component: str, version: str) -> str:
    parse_version(version)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", component):
        raise ComponentError(f"Invalid component id: {component!r}")
    return f"{component}.{version}"


def parse_tag(tag: str) -> tuple[str, str] | None:
    """``brands.0.0.4.15`` -> (brands, 0.0.4.15); historical shared tags return None."""
    match = TAG_RE.fullmatch(tag)
    if not match:
        return None
    return match.group(1), match.group(2)


def component_versions(component: str, tags: list[str]) -> list[str]:
    versions = [parsed[1] for parsed in map(parse_tag, tags) if parsed and parsed[0] == component]
    return sorted(versions, key=parse_version)


def check_release(component: str, version: str, sha: str, tags: dict[str, str]) -> str:
    """Refuse a collision or a version that does not move forward; ``tags`` maps tag -> commit.

    Publishing the same tag for the same commit again is idempotent (returns the tag).
    """
    tag = tag_name(component, version)
    if tag in tags:
        if tags[tag] != sha:
            raise ComponentError(f"Tag {tag} already points to {tags[tag]}, not {sha}")
        return tag
    published = component_versions(component, list(tags))
    if published and parse_version(version) <= parse_version(published[-1]):
        raise ComponentError(f"{component} {version} is not newer than published {published[-1]}")
    return tag


def transition_version(component: str, base: str | None, tags: list[str]) -> str:
    """First independent version: next free after the base, or 0.0.0.1 for a new module."""
    taken = set(component_versions(component, tags))
    candidate = next_version(base) if base else FIRST_VERSION
    while candidate in taken:
        candidate = next_version(candidate)
    return candidate


# -- registry ----------------------------------------------------------------------------

def load_registry(path: Path = REGISTRY_PATH) -> dict:
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("format_version") != 1 or not isinstance(registry.get("components"), dict):
        raise ComponentError("Unsupported component registry")
    return registry


def _matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def declared_version(component: str, root: Path = ROOT, registry: dict | None = None) -> str:
    """The version the component's own source declares (VERSION or its manifest)."""
    import yaml
    registry = registry or load_registry()
    source = root / registry["components"][component]["version_source"]
    if source.name == "VERSION":
        return source.read_text(encoding="utf-8").strip()
    manifest = yaml.safe_load(source.read_text(encoding="utf-8"))
    return str(manifest.get("version") or (manifest.get("module") or {}).get("version") or "")


# -- change impact --------------------------------------------------------------------------

@dataclass
class Impact:
    full_suite: bool = False
    components: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"full_suite": self.full_suite, "components": self.components, "tests": self.tests,
                "reasons": self.reasons}


def impact(changed: list[str], registry: dict | None = None) -> Impact:
    """Which components a change touches and which tests it must run (#509 CI rule).

    * a component's own files -> its tests, the compatibility checks with supported CORE
      and the install/update/rollback lifecycle tests;
    * a shared contract (module API, protocol, packaging, extension UI) -> every module;
    * a CORE change or a file no component claims (impact unknown) -> the full suite.
    """
    registry = registry or load_registry()
    components = registry["components"]
    result = Impact()
    touched: dict[str, list[str]] = {}

    def note(component: str, path: str) -> None:
        touched.setdefault(component, []).append(path)

    for path in sorted({p.strip().lstrip("./") for p in changed if p.strip()}):
        if _matches(path, registry.get("no_test_impact", [])):
            result.ignored.append(path)
            continue
        if _matches(path, registry["shared_contracts"]):
            for component, spec in components.items():
                if spec["kind"] == "module":
                    note(component, path)
            result.reasons.setdefault("shared_contract", []).append(path)
            continue
        if path.startswith("tests/"):
            owners = [c for c, spec in components.items() if path in spec.get("tests", [])]
            if path in registry["compatibility_tests"]:
                owners = [c for c, spec in components.items() if spec["kind"] == "module"]
            if not owners:
                result.full_suite = True
                result.reasons.setdefault("unknown", []).append(path)
            for owner in owners:
                note(owner, path)
            continue
        owners = [c for c, spec in components.items() if _matches(path, spec.get("paths", []))]
        # A module's own directory wins over a broader CORE pattern for the same file.
        modules = [c for c in owners if components[c]["kind"] == "module"]
        owners = modules or owners
        if not owners:
            result.full_suite = True
            result.reasons.setdefault("unknown", []).append(path)
            continue
        for owner in owners:
            note(owner, path)
            if components[owner].get("full_suite"):
                result.full_suite = True
                result.reasons.setdefault("core", []).append(path)

    result.components = sorted(touched)
    for component in result.components:
        result.reasons.setdefault(f"component:{component}", []).extend(touched[component])
    if not result.full_suite:
        tests: set[str] = set()
        for component in result.components:
            tests.update(components[component].get("tests", []))
        if result.components:
            tests.update(registry["compatibility_tests"])
        result.tests = sorted(tests)
    return result


# -- history (transition) --------------------------------------------------------------------

def _changelog_entries(changelog: str) -> list[tuple[str, str | None]]:
    """CHANGELOG entries oldest-first: (correct version, original Git subject or None)."""
    entries: list[list] = []
    for line in changelog.splitlines():
        if line.startswith("## ПРАВИЛЬНА НАЗВА:"):
            entries.append([line.split(":", 1)[1].strip(), None])
        elif line.startswith("## ОРИГІНАЛЬНА НАЗВА:") and entries:
            entries[-1][1] = line.split(":", 1)[1].strip()
        elif re.match(r"^## \d+\.\d+\.\d+\.\d+(\s|$)", line):
            entries.append([line[3:].split()[0], None])
    return [(correct, original) for correct, original in reversed(entries)]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[’ʼ`´]", "'", text)).strip()


def _same_subject(subject: str, label: str) -> bool:
    """The Git subject of an entry; typography may differ in a «version — description» subject."""
    if _normalize(subject) == _normalize(label):
        return True
    head = re.match(r"^(\d+\.\d+\.\d+\.\d+) [—-] ", _normalize(label))
    return bool(head) and _normalize(subject).startswith(head.group(1) + " ")


def shared_releases(log: list[tuple[str, str, list[str]]], changelog: str = "") -> list[tuple[str, list[str]]]:
    """Shared Vertep versions in Git order, with the files each release changed.

    ``log`` is oldest-first (sha, subject, files). CHANGELOG is the authority: every entry
    finds its commit by subject — the original subject for a historical misnamed commit
    (``ОРИГІНАЛЬНА НАЗВА``), the version otherwise; repeated subjects are matched in order.
    Files of commits between two version commits accumulate into the next one.
    """
    version_of: dict[int, str] = {}
    if changelog:
        by_subject: dict[str, list[int]] = {}
        for position, (_sha, subject, _files) in enumerate(log):
            by_subject.setdefault(_normalize(subject), []).append(position)
        used: set[int] = set()
        for correct, original in _changelog_entries(changelog):
            label = original or correct
            candidates = [p for p in by_subject.get(_normalize(label), []) if p not in used]
            if not candidates:
                candidates = [p for p, (_sha, subject, _files) in enumerate(log)
                              if p not in used and _same_subject(subject, label)]
            if candidates:
                used.add(candidates[0])
                version_of[candidates[0]] = correct
    else:
        version_of = {p: subject.strip() for p, (_sha, subject, _files) in enumerate(log)
                      if VERSION_RE.fullmatch(subject.strip())}
    releases: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for position, (_sha, _subject, files) in enumerate(log):
        pending.extend(files)
        if position in version_of:
            releases.append((version_of[position], pending))
            pending = []
    return releases


def last_shared_change(component: str, releases: list[tuple[str, list[str]]], registry: dict | None = None) -> str | None:
    registry = registry or load_registry()
    spec = registry["components"][component]
    paths, excluded = spec.get("paths", []), spec.get("history_exclude", [])
    base = None
    for version, files in releases:
        if any(_matches(path, paths) and not _matches(path, excluded) for path in files):
            base = version
    return base


def history_mapping(releases: list[tuple[str, list[str]]], tags: list[str], registry: dict | None = None) -> dict:
    """«shared release -> component version» for the transition, per module."""
    registry = registry or load_registry()
    result = {}
    for component, spec in registry["components"].items():
        if spec["kind"] != "module":
            continue
        base = last_shared_change(component, releases, registry)
        result[component] = {"base": base, "first_independent": transition_version(component, base, tags),
                             "legacy_versions": spec.get("legacy_versions", {})}
    return result


# -- signed component manifest -----------------------------------------------------------------

def _canonical(document: dict) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_manifest(component: str, version: str, commit: str, artifacts: list[dict], *,
                   compatibility: dict, dependencies: list[dict], registry: dict | None = None,
                   now: datetime | None = None) -> dict:
    registry = registry or load_registry()
    if component not in registry["components"]:
        raise ComponentError(f"Unknown component: {component}")
    parse_version(version)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ComponentError("Commit must be a full SHA")
    for artifact in artifacts:
        if not (re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", "")))
                or re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", "")))):
            raise ComponentError(f"Artifact without sha256/digest: {artifact}")
    return {"format_version": MANIFEST_FORMAT, "component": component,
            "kind": registry["components"][component]["kind"], "version": version,
            "tag": tag_name(component, version), "channel": "stable",
            "source": {"repository": registry["source_repository"], "commit": commit},
            "artifacts": artifacts, "compatibility": compatibility, "dependencies": dependencies,
            "created_at": (now or datetime.now(timezone.utc)).isoformat()}


def sign_manifest(manifest: dict, private_key_pem: bytes) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ComponentError("Component signing key must be RSA")
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    signed["signature"] = base64.b64encode(key.sign(_canonical(signed), padding.PKCS1v15(), hashes.SHA256())).decode()
    return signed


def verify_manifest(manifest: dict, public_key_pem: bytes, *, artifacts: dict[str, bytes] | None = None,
                    registry: dict | None = None) -> dict:
    """Signature, identity, stable channel, official source and artifact hashes."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    registry = registry or load_registry()
    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
        serialization.load_pem_public_key(public_key_pem).verify(signature, _canonical(manifest),
                                                                  padding.PKCS1v15(), hashes.SHA256())
    except (KeyError, TypeError, ValueError, InvalidSignature) as error:
        raise ComponentError("Component manifest signature is not valid") from error
    if manifest.get("format_version") != MANIFEST_FORMAT:
        raise ComponentError("Unsupported component manifest format")
    if manifest.get("channel") != "stable":
        raise ComponentError("Only stable component releases are accepted")
    if manifest.get("component") not in registry["components"]:
        raise ComponentError("Unknown component")
    if manifest.get("tag") != tag_name(manifest["component"], manifest["version"]):
        raise ComponentError("Tag does not match component and version")
    if manifest.get("source", {}).get("repository") != registry["source_repository"]:
        raise ComponentError("Official components come only from the Vertep repository")
    for artifact in manifest.get("artifacts", []):
        if artifacts is not None and "sha256" in artifact:
            content = artifacts.get(artifact["name"])
            if content is None or hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                raise ComponentError(f"Artifact {artifact.get('name')} does not match its hash")
    return manifest


# -- module packages (#510/#511) ----------------------------------------------------------------
# A component release of a module carries a signed module package (``package.json``): the
# manifest (with the container image pinned to the digest built for this release), its migration
# SQL and its UI bundle, signed with the official Ed25519 publisher key. CORE installs or updates
# it through the Module Registry source like any external package (core.module.packages).

PUBLISHER_TRUST = Path(__file__).resolve().parents[1] / "config" / "module-trust" / "publishers.json"


def module_manifest_document(component: str, root: Path | None = None, registry: dict | None = None) -> dict:
    import yaml
    registry = registry or load_registry()
    spec = registry["components"].get(component)
    if not spec or spec["kind"] != "module":
        raise ComponentError(f"{component} is not a module component")
    # A role module that moved into its own container (Publisher, 21.11.2026) keeps the role
    # manifest for the release it ships with and packages its application manifest.
    source = spec.get("package_manifest") or spec["version_source"]
    data = yaml.safe_load(((root or ROOT) / source).read_text(encoding="utf-8"))
    return data.get("module") if isinstance(data.get("module"), dict) else data.get("manifest", data)


PINNED_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")


def build_package(component: str, *, image: str | None = None, extra_images: dict[str, str] | None = None,
                  root: Path | None = None, registry: dict | None = None) -> dict:
    """Unsigned package: manifest + every file CORE needs (migrations, UI bundle).

    A component with several images (LAB: lab-control + lab-web, #491 L5) also carries
    ``images.json`` (service -> image pinned by digest) and its ``package_files``
    (the runtime compose file), all covered by the signature.
    """
    root = root or ROOT
    registry = registry or load_registry()
    spec = registry["components"][component]
    manifest = dict(module_manifest_document(component, root, registry))
    module_dir = (root / spec["version_source"]).parent
    if image is not None:
        if not PINNED_IMAGE.fullmatch(image):
            raise ComponentError("Package image must be pinned by digest")
        if not manifest.get("container"):
            raise ComponentError(f"{component} has no container to pin")
        manifest["container"] = {**manifest["container"], "image": image}
    files: dict[str, str] = {}
    for migration in manifest.get("migrations") or []:
        path = module_dir / "migrations" / f"{migration['id']}.sql"
        if not path.is_file():
            raise ComponentError(f"{component}: migration {path.name} is missing")
        files[f"migrations/{migration['id']}.sql"] = base64.b64encode(path.read_bytes()).decode("ascii")
    ui = manifest.get("ui") or {}
    for name, digest in (ui.get("files") or {}).items():
        data = (module_dir / "ui" / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ComponentError(f"{component}: ui/{name} does not match the manifest digest")
        files[name] = base64.b64encode(data).decode("ascii")
    for name, source in (spec.get("package_files") or {}).items():
        files[name] = base64.b64encode((root / source).read_bytes()).decode("ascii")
    if spec.get("extra_images") and image is not None:
        images = {spec["image"]["service"]: image, **(extra_images or {})}
        expected = {spec["image"]["service"], *(item["service"] for item in spec["extra_images"])}
        if set(images) != expected or not all(PINNED_IMAGE.fullmatch(ref) for ref in images.values()):
            raise ComponentError(f"{component}: every image ({', '.join(sorted(expected))}) must be pinned by digest")
        files["images.json"] = base64.b64encode(json.dumps(images, sort_keys=True).encode()).decode("ascii")
    return {"manifest": manifest, "files": files}


def sign_package(package: dict, private_key_pem: bytes, key_id: str) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ComponentError("Module package signing key must be Ed25519")
    content = {"manifest": package["manifest"], "files": package["files"]}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {**content, "signer": key_id, "signature": base64.b64encode(key.sign(raw)).decode("ascii")}
