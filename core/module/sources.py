"""Module sources, trust checks, access profiles, CORE package cache and threat blocking (#511).

* A module is installed from the Module Registry (a JSON index) or a GitHub repository URL
  (stable releases tagged ``<module>.<version>``, #510). The verified source is pinned;
  later updates come only from that source.
* Before the administrator approves an install or update, the package is assessed:
  stable release, signature and provenance (``verify_package``), package hash, CORE
  compatibility, dependencies, permissions with their access profile
  (minimal/standard/elevated) and known vulnerabilities from the signed advisory feed.
* Verified packages are kept in the CORE cache (by sha256) and reused for repeats and nodes.
* A module that lost support keeps working (warning only). A confirmed critical threat
  blocks the installed module at once (protective block, no approval wait).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .manifest import ModuleManifest

ROOT = Path(__file__).resolve().parents[2]
PROFILES_PATH = ROOT / "config" / "module-permission-profiles.json"
ADVISORY_KEY = ROOT / "installer" / "update-public.pem"
TAG_RE = r"^{module}\.(\d+\.\d+\.\d+\.\d+)$"
MAX_DOWNLOAD = 64 * 1024 * 1024


class SourceError(ValueError):
    pass


# -- sources -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    type: str        # "registry" | "github"
    location: str    # registry index URL | "owner/repo"

    def to_dict(self) -> dict:
        return {"type": self.type, "location": self.location}


def parse_source(value) -> Source:
    """``https://github.com/o/r``, ``github:o/r``, a registry index URL, or {"type", "location"}."""
    if isinstance(value, dict):
        kind, location = str(value.get("type", "")), str(value.get("location", "")).strip()
        if kind == "github":
            return parse_source(f"github:{location}")
        if kind == "registry":
            return parse_source(location)
        raise SourceError("Source type must be registry or github")
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:github:|https://github\.com/)([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?", text)
    if match:
        return Source("github", f"{match.group(1)}/{match.group(2)}")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return Source("registry", text)
    raise SourceError("Source must be a GitHub repository URL or an https Module Registry index")


def _source_timeout() -> float:
    import os
    try:
        return max(1.0, min(30.0, float(os.getenv("VERTEP_MODULE_SOURCE_TIMEOUT_SECONDS", "10"))))
    except ValueError:
        return 10.0


def _default_fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SourceError("Only https sources are allowed")
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json, application/json",
                                                   "User-Agent": "vertep-core"})
    with urllib.request.urlopen(request, timeout=_source_timeout()) as response:  # noqa: S310 (https enforced)
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise SourceError("Package is too large")
    return data


class SourceResolver:
    """Lists releases of a module in a source and downloads its package bytes."""

    def __init__(self, fetch: Callable[[str], bytes] = _default_fetch) -> None:
        self.fetch = fetch

    def _fetch_cached(self, url: str, url_cache: dict[str, bytes] | None) -> bytes:
        if url_cache is not None and url in url_cache:
            return url_cache[url]
        data = self.fetch(url)
        if url_cache is not None:
            url_cache[url] = data
        return data

    def releases(self, source: Source, module_id: str, *, _url_cache: dict[str, bytes] | None = None) -> list[dict]:
        if source.type == "registry":
            index = json.loads(self._fetch_cached(source.location, _url_cache))
            entries = (index.get("modules") or {}).get(module_id) or []
            result = []
            for entry in entries:
                url = urllib.parse.urljoin(source.location, str(entry.get("package_url", "")))
                result.append({"version": str(entry.get("version")), "stable": entry.get("channel", "stable") == "stable",
                               "package_url": url, "sha256": entry.get("sha256"),
                               "unsupported": bool(entry.get("unsupported"))})
            return result
        owner_repo = source.location
        releases = json.loads(
            self._fetch_cached(f"https://api.github.com/repos/{owner_repo}/releases?per_page=100", _url_cache)
        )
        pattern = re.compile(TAG_RE.format(module=re.escape(module_id)))
        result = []
        for release in releases:
            match = pattern.fullmatch(str(release.get("tag_name", "")))
            if not match or release.get("draft"):
                continue
            assets = {asset.get("name"): asset for asset in release.get("assets") or []}
            package = assets.get("package.json")
            if not package:
                continue
            digest = None
            if "package.json.sha256" in assets:
                digest = self._fetch_cached(
                    assets["package.json.sha256"]["browser_download_url"], _url_cache
                ).decode().split()[0]
            result.append({"version": match.group(1), "stable": not release.get("prerelease"),
                           "package_url": package["browser_download_url"], "sha256": digest, "unsupported": False})
        return result

    def resolve(self, source: Source, module_id: str, version: str | None = None) -> dict:
        from core.components import parse_version
        candidates = self.releases(source, module_id)
        if version is not None:
            found = [item for item in candidates if item["version"] == version]
            if not found:
                raise SourceError(f"{module_id} {version} is not published in {source.location}")
            return found[0]
        stable = [item for item in candidates if item["stable"]]
        if not stable:
            raise SourceError(f"No stable release of {module_id} in {source.location}")

        def key(item):
            try:
                return (1, parse_version(item["version"]))
            except ValueError:
                return (0, tuple(int(p) for p in re.findall(r"\d+", item["version"])))
        return max(stable, key=key)

    def download(self, release: dict) -> bytes:
        data = self.fetch(release["package_url"])
        if release.get("sha256") and hashlib.sha256(data).hexdigest() != release["sha256"]:
            raise SourceError("Downloaded package does not match its published hash")
        return data


# -- CORE package cache --------------------------------------------------------------------------

class PackageCache:
    """Verified packages on CORE, keyed by module, version and sha256 (served to nodes too)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, module_id: str, version: str, digest: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", module_id) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SourceError("Invalid cache key")
        return self.root / module_id / f"{version}-{digest}.json"

    def put(self, module_id: str, version: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(module_id, version, digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(data)
            temporary.replace(path)
        return digest

    def get(self, module_id: str, version: str, digest: str | None = None) -> bytes | None:
        folder = self.root / module_id
        for path in sorted(folder.glob(f"{version}-*.json")) if folder.is_dir() else []:
            data = path.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            if path.stem.endswith(actual) and (digest is None or digest == actual):
                return data
        return None

    def prune(self, module_id: str, keep: list[str]) -> list[str]:
        """Keep packages of the given versions (active + two previous successful, #513)."""
        folder = self.root / module_id
        removed = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            if path.name.rsplit("-", 1)[0] not in keep:
                path.unlink()
                removed.append(path.name)
        return removed

    def entries(self, module_id: str) -> list[str]:
        folder = self.root / module_id
        return sorted(path.name for path in folder.glob("*.json")) if folder.is_dir() else []


# -- access profiles -----------------------------------------------------------------------------

def access_profile(manifest: ModuleManifest, table: dict | None = None) -> str:
    table = table or json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    order = table["order"]
    level = 0
    for permission in manifest.permissions:
        found = next((name for name in order if permission in table["profiles"].get(name, [])), "elevated")
        level = max(level, order.index(found))
    if manifest.secret_refs or any(item.get("type") == "capability" for item in manifest.requires or []):
        level = order.index("elevated")
    return order[level]


# -- advisories ------------------------------------------------------------------------------------

def _canonical(document: dict) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_advisories(document: dict, private_key_pem: bytes) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signed = {k: v for k, v in document.items() if k != "signature"}
    signed["signature"] = base64.b64encode(key.sign(_canonical(signed), padding.PKCS1v15(), hashes.SHA256())).decode()
    return signed


def verify_advisories(document: dict, public_key_pem: bytes | None = None) -> list[dict]:
    """The signed advisory feed; an unsigned or altered feed is refused entirely."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    public = public_key_pem or ADVISORY_KEY.read_bytes()
    try:
        serialization.load_pem_public_key(public).verify(base64.b64decode(document["signature"], validate=True),
                                                         _canonical(document), padding.PKCS1v15(), hashes.SHA256())
    except (KeyError, TypeError, ValueError, InvalidSignature) as error:
        raise SourceError("Advisory feed signature is not valid") from error
    advisories = document.get("advisories")
    if document.get("format_version") != 1 or not isinstance(advisories, list):
        raise SourceError("Unsupported advisory feed")
    return advisories


def matching_advisories(advisories: list[dict], module_id: str, version: str) -> list[dict]:
    return [item for item in advisories if item.get("module") == module_id
            and (item.get("versions") == "all" or version in (item.get("versions") or []))]


def _blocking(advisories: list[dict]) -> list[dict]:
    """Only a confirmed critical threat blocks without waiting for approval."""
    return [item for item in advisories if item.get("severity") == "critical" and item.get("confirmed")]


# -- assessment ----------------------------------------------------------------------------------

def assess(package: dict, release: dict, *, verify: Callable[[dict], ModuleManifest], core_version: str | None,
           active: dict[str, str], advisories: list[dict], data: bytes | None = None) -> dict:
    """Everything the administrator sees before approving; ``ok`` only if every check passed."""
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("stable", release.get("stable", False), "" if release.get("stable") else "pre-release or unstable channel")
    if release.get("sha256") and data is not None:
        actual = hashlib.sha256(data).hexdigest()
        check("hash", actual == release["sha256"], actual)
    try:
        manifest = verify(package)
        check("signature", True, f"publisher {manifest.publisher or '-'}")
    except Exception as error:  # noqa: BLE001 - every refusal is reported, nothing installs
        check("signature", False, str(error))
        return {"ok": False, "checks": checks, "module_id": None, "version": release.get("version"),
                "profile": None, "warnings": []}
    check("version", manifest.version == release.get("version"), f"package {manifest.version}")
    check("compatibility", manifest.core_compatible(core_version), f"CORE {core_version}")
    missing = []
    for dependency in manifest.dependencies:
        name = dependency.get("module") or dependency.get("name") if isinstance(dependency, dict) else str(dependency)
        if name and name not in active:
            missing.append(name)
    check("dependencies", not missing, ", ".join(missing))
    profile = access_profile(manifest)
    check("permissions", True, f"{profile}: {', '.join(manifest.permissions) or 'none'}")
    affecting = matching_advisories(advisories, manifest.module_id, manifest.version)
    threats = [item for item in affecting if item.get("severity") in {"high", "critical"}]
    check("vulnerabilities", not threats, ", ".join(item.get("id", "?") for item in threats))
    warnings = []
    if release.get("unsupported") or any(item.get("unsupported") for item in affecting):
        warnings.append("Модуль більше не підтримується видавцем; він продовжує працювати.")
    return {"ok": all(item["ok"] for item in checks), "checks": checks, "module_id": manifest.module_id,
            "version": manifest.version, "profile": profile, "permissions": manifest.permissions,
            "warnings": warnings}


# -- orchestration -------------------------------------------------------------------------------

def _log(message: str, operation_id: str | None, **fields) -> None:
    from .update_manager import update_log
    update_log(message, operation_id, **fields)


# #553 AC5: the API builds a new ModuleSources per request, so the recommendations snapshot
# lives here, one per lifecycle root, and survives between requests.
_SNAPSHOTS: dict[str, dict] = {}
_SNAPSHOTS_LOCK = threading.Lock()


class ModuleSources:
    """Install/update from pinned sources through the platform lifecycle."""

    def __init__(self, lifecycle, resolver: SourceResolver | None = None, *, core_version: str | None = None,
                 advisories: Callable[[], list[dict]] = lambda: []) -> None:
        self.lifecycle = lifecycle
        self.resolver = resolver or SourceResolver()
        self.cache = PackageCache(Path(lifecycle.root) / "cache")
        self.pins = Path(lifecycle.root) / "sources"
        self.core_version = core_version
        self.advisories = advisories

    def pin(self, module_id: str) -> dict | None:
        path = self.pins / f"{module_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return self._official_pin(module_id)

    def _official_pin(self, module_id: str) -> dict | None:
        """Official modules installed with the release update from the official repository (21.11.2026).

        Only modules whose component releases carry a signed package (their own container,
        ``image`` in config/components.json). The pin is implicit until the first update
        writes a real one; the package is still verified against the trusted publisher key.
        """
        from core.components import load_registry
        from .lifecycle import BUNDLED
        record = self.lifecycle.record(module_id)
        if module_id not in BUNDLED or record is None:
            return None
        registry = load_registry()
        if not (registry["components"].get(module_id) or {}).get("image"):
            return None
        return {"module_id": module_id, "source": {"type": "github", "location": registry["source_repository"]},
                "version": record["version"], "official": True}

    def _save_pin(self, module_id: str, source: Source, version: str, digest: str) -> None:
        self.pins.mkdir(parents=True, exist_ok=True)
        path = self.pins / f"{module_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"module_id": module_id, "source": source.to_dict(), "version": version,
                                         "sha256": digest, "pinned_at": time.time()}, indent=2), encoding="utf-8")
        temporary.replace(path)
        self._snapshot_state()["checked_at"] = None  # a new pin makes the recommendations out of date

    def _snapshot_state(self) -> dict:
        with _SNAPSHOTS_LOCK:
            return _SNAPSHOTS.setdefault(str(Path(self.lifecycle.root).resolve()),
                                         {"recommendations": [], "checked_at": None, "refreshing": False})

    def _save_history(self, module_id: str, history: list[str]) -> None:
        path = self.pins / f"{module_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["history"] = history  # previous successful versions, newest last
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _active(self) -> dict[str, str]:
        return {module_id: record["version"] for module_id, record in self.lifecycle.records().items()
                if record.get("state") == "ACTIVE"}

    def _fetch(self, source: Source, module_id: str, version: str | None) -> tuple[dict, bytes, dict]:
        release = self.resolver.resolve(source, module_id, version)
        data = self.cache.get(module_id, release["version"], release.get("sha256"))
        if data is None:
            data = self.resolver.download(release)
        try:
            package = json.loads(data)
        except ValueError as error:
            raise SourceError("Package is not valid JSON") from error
        return release, data, package

    def assess(self, source, module_id: str, version: str | None = None) -> dict:
        source = parse_source(source)
        release, data, package = self._fetch(source, module_id, version)
        report = assess(package, release, verify=self.lifecycle.verify, core_version=self.core_version,
                        active=self._active(), advisories=self.advisories(), data=data)
        return {**report, "source": source.to_dict()}

    def _approved(self, report: dict, approved_profile: str | None) -> None:
        if not report["ok"]:
            failed = [f"{item['name']}: {item['detail']}" for item in report["checks"] if not item["ok"]]
            raise SourceError("Package refused: " + "; ".join(failed))
        if approved_profile != report["profile"]:
            raise SourceError(f"Administrator approval for the {report['profile']} access profile is required")

    def install(self, source, module_id: str, version: str | None = None, *, approved_profile: str | None) -> dict:
        source = parse_source(source)
        release, data, package = self._fetch(source, module_id, version)
        report = assess(package, release, verify=self.lifecycle.verify, core_version=self.core_version,
                        active=self._active(), advisories=self.advisories(), data=data)
        self._approved(report, approved_profile)
        if report["module_id"] != module_id:
            raise SourceError("Package is a different module")
        digest = self.cache.put(module_id, report["version"], data)
        result = self.lifecycle.install(package)
        self._save_pin(module_id, source, report["version"], digest)
        _log("module installed from its source", None, module=module_id, version=report["version"],
             source=source.to_dict(), profile=report["profile"])
        return {**result, "version": report["version"], "profile": report["profile"], "source": source.to_dict(),
                "warnings": report["warnings"]}

    def update(self, module_id: str, version: str | None = None, *, approved_profile: str | None,
               source=None) -> dict:
        pin = self.pin(module_id)
        if pin is None:
            raise SourceError("Module has no pinned source; it is updated with its own release channel")
        pinned = parse_source(pin["source"])
        if source is not None and parse_source(source) != pinned:
            raise SourceError("Updates come only from the module's pinned source")
        release, data, package = self._fetch(pinned, module_id, version)
        report = assess(package, release, verify=self.lifecycle.verify, core_version=self.core_version,
                        active=self._active(), advisories=self.advisories(), data=data)
        self._approved(report, approved_profile)
        # The administrator approved this package's access profile: its declared permissions
        # are granted with it (a role module moving into its own container gains some).
        registry = getattr(self.lifecycle, "registry", None)
        if registry is not None:
            declared = self.lifecycle.verify(package).permissions
            approved = set(registry.placement.approvals.get(module_id, []))
            if set(declared) - approved:
                registry.placement.set_approval(module_id, sorted(approved | set(declared)))
        digest = self.cache.put(module_id, report["version"], data)
        result = self.lifecycle.update(package, confirmed=True)
        self._save_pin(module_id, pinned, report["version"], digest)
        _log("module updated from its pinned source", result.get("operation"), module=module_id,
             version=report["version"], profile=report["profile"])
        history = pin.get("history", [])[-1:] + [pin["version"]]
        self._save_history(module_id, history)
        self.cache.prune(module_id, keep=[report["version"], *history[-2:]])
        record = self.lifecycle.record(module_id) or {}
        if record.get("blocked") and not _blocking(matching_advisories(self.advisories(), module_id, report["version"])):
            self.lifecycle.unblock(module_id, reason="updated to an unaffected version")
        return {**result, "version": report["version"], "profile": report["profile"], "source": pinned.to_dict()}

    def enforce(self) -> list[dict]:
        """Protective block of installed modules hit by a confirmed critical threat (no approval wait)."""
        blocked = []
        advisories = self.advisories()
        for module_id, record in self.lifecycle.records().items():
            for advisory in _blocking(matching_advisories(advisories, module_id, record["version"])):
                if not record.get("blocked"):
                    self.lifecycle.block(module_id, advisory.get("id", "critical advisory"))
                    _log("module blocked by a confirmed critical threat", None, module=module_id,
                         version=record["version"], advisory=advisory.get("id"))
                    blocked.append({"module_id": module_id, "version": record["version"], "advisory": advisory.get("id")})
        return blocked


    def recommendations(self) -> list[dict]:
        """#515: newer stable releases of modules with a pinned source, assessed with their risk."""
        from core.components import parse_version
        result = []
        for module_id, record in sorted(self.lifecycle.records().items()):
            pin = self.pin(module_id)
            if pin is None:
                continue
            entry = {"module_id": module_id, "current": record["version"], "source": pin["source"],
                     "available": None, "recommended": False, "risk": None, "reasons": []}
            try:
                releases = [r for r in self.resolver.releases(parse_source(pin["source"]), module_id) if r["stable"]]

                def key(item):
                    try:
                        return parse_version(item["version"])
                    except ValueError:
                        return (0, 0, 0, 0)
                newer = [r for r in releases if key(r) > key({"version": record["version"]})]
                if not newer and self.lifecycle._manifest(record).kind == "execution" and "bundled" in (record.get("package") or {}):
                    # A bundled role and its container package share one version (#511).
                    newer = [r for r in releases if r["version"] == record["version"]]
                if not newer:
                    result.append({**entry, "reasons": ["Встановлено найновішу стабільну версію"]})
                    continue
                target = max(newer, key=key)
                report = self.assess(pin["source"], module_id, target["version"])
            except (SourceError, OSError, ValueError) as error:
                result.append({**entry, "risk": "unknown", "reasons": [f"Джерело недоступне: {error}"]})
                continue
            current_profile = access_profile(self.lifecycle._manifest(record))
            reasons = [f"{c['name']}: {c['detail']}" for c in report["checks"] if not c["ok"]]
            fixes = [a.get("id") for a in matching_advisories(self.advisories(), module_id, record["version"])
                     if a.get("severity") in {"high", "critical"}]
            if not report["ok"]:
                risk = "high"
            elif report["profile"] != current_profile:
                risk = "medium"
                reasons.append(f"Профіль доступу змінюється: {current_profile} → {report['profile']}")
            else:
                risk = "low"
            if fixes and report["ok"]:
                reasons.append(f"Усуває вразливості встановленої версії: {', '.join(fixes)}")
            result.append({**entry, "available": target["version"], "recommended": report["ok"],
                           "risk": risk, "profile": report["profile"], "current_profile": current_profile,
                           "urgent": bool(fixes) and report["ok"], "checks": report["checks"],
                           "warnings": report["warnings"], "reasons": reasons})
        return result

    def recommendations_snapshot(self, *, refresh: bool = False) -> dict:
        """#553 AC5: an ordinary GET never waits for the pinned sources.

        The last computed recommendations are returned at once (``stale`` once older than
        ``VERTEP_RECOMMENDATIONS_TTL_SECONDS``, 300 by default). A cold or stale cache starts a
        background refresh with the bounded source timeout; ``refresh=True`` computes and stores
        the result synchronously.
        """
        import os
        try:
            ttl = max(1.0, float(os.getenv("VERTEP_RECOMMENDATIONS_TTL_SECONDS", "300")))
        except ValueError:
            ttl = 300.0
        lock = _SNAPSHOTS_LOCK
        state = self._snapshot_state()

        def compute() -> None:
            try:
                items = self.recommendations()
                with lock:
                    state.update({"recommendations": items, "checked_at": time.time()})
            finally:
                with lock:
                    state["refreshing"] = False

        if refresh:
            with lock:
                state["refreshing"] = True
            compute()
        else:
            with lock:
                age = None if state["checked_at"] is None else time.time() - state["checked_at"]
                start = (age is None or age > ttl) and not state["refreshing"]
                if start:
                    state["refreshing"] = True
            if start:
                threading.Thread(target=compute, name="module-recommendations", daemon=True).start()
        with lock:
            age = None if state["checked_at"] is None else time.time() - state["checked_at"]
            return {"recommendations": list(state["recommendations"]), "checked_at": state["checked_at"],
                    "stale": age is None or age > ttl, "refreshing": state["refreshing"]}
