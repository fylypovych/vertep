"""#511: install/update from the Module Registry or GitHub, pinned source, trust checks,
access profiles, CORE package cache and protective blocking on a confirmed threat."""

import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.module.lifecycle import LifecycleError, ModuleLifecycle
from core.module.packages import verify_package
from core.module.sources import (ModuleSources, SourceError, SourceResolver, access_profile, parse_source,
                                 sign_advisories, verify_advisories)
from core.module.trust import Publisher, TrustStore
from tests.test_module_trust_isolation import CONTAINER, pem, sign

REGISTRY = "https://registry.example/vertep/index.json"
GITHUB = "https://github.com/acme/notes"


def package(key, version, permissions=None, repository="github.com/acme/notes", name="notes"):
    manifest = {"schema_version": "2.0", "name": name, "version": version, "capabilities": [], "kind": "application",
                "publisher": "acme", "provenance": {"repository": repository}, "container": CONTAINER,
                "core_compatibility": {"min": "0.0.4.0", "max": "0.0.9.99"},
                "permissions": permissions or [], "migrations": []}
    return json.dumps(sign(key, manifest, {})).encode()


class Network:
    """A fake https network: url -> bytes; counts package downloads."""

    def __init__(self):
        self.files, self.downloads = {}, []

    def __call__(self, url):
        if url not in self.files:
            raise SourceError(f"404 {url}")
        if url.endswith(".pkg"):
            self.downloads.append(url)
        return self.files[url]

    def registry(self, entries):
        self.files[REGISTRY] = json.dumps({"format_version": 1, "modules": {"notes": entries}}).encode()

    def github(self, releases):
        self.files["https://api.github.com/repos/acme/notes/releases?per_page=100"] = json.dumps(releases).encode()


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def feed_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()),
            private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


@pytest.fixture
def world(tmp_path, key, feed_key):
    trust = TrustStore({"acme": Publisher("acme", {"acme-k1": pem(key)}, ["github.com/acme/notes"])})
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda p: verify_package(p, trust), drain_timeout=0.1,
                                system_versions={"core_runtime": "0.0.4.20"})
    network = Network()
    feed = {"document": {"format_version": 1, "advisories": []}}
    sources = ModuleSources(lifecycle, SourceResolver(network), core_version="0.0.4.20",
                            advisories=lambda: verify_advisories(sign_advisories(feed["document"], feed_key[0]),
                                                                 feed_key[1]))
    return {"lifecycle": lifecycle, "network": network, "sources": sources, "feed": feed, "key": key}


def publish_registry(world, *versions, permissions=None, tamper=None):
    entries = []
    for version in versions:
        data = package(world["key"], version, permissions)
        url = f"https://registry.example/vertep/notes-{version}.pkg"
        world["network"].files[url] = data
        entries.append({"version": version, "channel": "stable", "package_url": f"notes-{version}.pkg",
                        "sha256": tamper or hashlib.sha256(data).hexdigest()})
    world["network"].registry(entries)


def test_parse_source_accepts_registry_and_github_and_refuses_plain_http():
    assert parse_source(GITHUB).to_dict() == {"type": "github", "location": "acme/notes"}
    assert parse_source("github:acme/notes") == parse_source(GITHUB + ".git")
    assert parse_source(REGISTRY).type == "registry"
    for bad in ("http://registry.example/index.json", "https://user:pw@x/index.json", "ftp://x", ""):
        with pytest.raises(SourceError):
            parse_source(bad)


def test_install_from_the_module_registry_pins_the_source_and_uses_the_cache(world):
    publish_registry(world, "0.0.0.1")
    report = world["sources"].assess(REGISTRY, "notes")
    assert report["ok"] and report["profile"] == "minimal" and report["version"] == "0.0.0.1"
    assert {c["name"] for c in report["checks"]} >= {"stable", "hash", "signature", "compatibility",
                                                      "dependencies", "permissions", "vulnerabilities"}
    result = world["sources"].install(REGISTRY, "notes", approved_profile="minimal")
    assert result["state"] == "INSTALLED" and result["source"] == {"type": "registry", "location": REGISTRY}
    assert world["sources"].pin("notes")["version"] == "0.0.0.1"
    downloads = len(world["network"].downloads)
    world["sources"].assess(REGISTRY, "notes", "0.0.0.1")  # verified copy reused from the CORE cache
    assert len(world["network"].downloads) == downloads
    assert world["sources"].cache.entries("notes")


def test_install_from_a_github_url_takes_the_latest_stable_component_release(world):
    releases = []
    for version, prerelease in (("0.0.0.1", False), ("0.0.0.2", False), ("0.0.0.3", True)):
        url = f"https://github.com/acme/notes/releases/download/notes.{version}/package.json.pkg"
        world["network"].files[url] = package(world["key"], version)
        releases.append({"tag_name": f"notes.{version}", "prerelease": prerelease, "draft": False,
                         "assets": [{"name": "package.json", "browser_download_url": url}]})
    releases.append({"tag_name": "0.0.4.19", "assets": []})  # a runtime release of the same repo is ignored
    world["network"].github(releases)
    result = world["sources"].install(GITHUB, "notes", approved_profile="minimal")
    assert result["version"] == "0.0.0.2"  # 0.0.0.3 is a pre-release
    refused = world["sources"].assess(GITHUB, "notes", "0.0.0.3")
    assert not refused["ok"] and [c for c in refused["checks"] if c["name"] == "stable" and not c["ok"]]


def test_updates_come_only_from_the_pinned_source(world):
    publish_registry(world, "0.0.0.1")
    world["sources"].install(REGISTRY, "notes", approved_profile="minimal")
    with pytest.raises(SourceError, match="pinned source"):
        world["sources"].update("notes", approved_profile="minimal", source=GITHUB)
    publish_registry(world, "0.0.0.1", "0.0.0.2")
    updated = world["sources"].update("notes", approved_profile="minimal")
    assert updated["version"] == "0.0.0.2" and world["lifecycle"].record("notes")["version"] == "0.0.0.2"
    assert world["sources"].pin("notes")["source"]["type"] == "registry"


@pytest.mark.parametrize("case", ["hash", "signature", "fork", "core"])
def test_an_invalid_package_is_refused_and_nothing_is_installed(world, case):
    if case == "hash":
        publish_registry(world, "0.0.0.1", tamper="0" * 64)
        with pytest.raises(SourceError, match="hash"):
            world["sources"].install(REGISTRY, "notes", approved_profile="minimal")
    else:
        key = Ed25519PrivateKey.generate() if case == "signature" else world["key"]
        data = package(key, "0.0.0.1", repository="github.com/evil/notes-fork" if case == "fork" else "github.com/acme/notes")
        if case == "core":
            raw = json.loads(data)
            raw["manifest"]["core_compatibility"] = {"min": "0.0.9.0"}
            data = json.dumps(sign(world["key"], raw["manifest"], {})).encode()
        world["network"].files["https://registry.example/vertep/notes-0.0.0.1.pkg"] = data
        world["network"].registry([{"version": "0.0.0.1", "package_url": "notes-0.0.0.1.pkg",
                                    "sha256": hashlib.sha256(data).hexdigest()}])
        report = world["sources"].assess(REGISTRY, "notes")
        failed = {c["name"] for c in report["checks"] if not c["ok"]}
        assert not report["ok"] and failed == ({"compatibility"} if case == "core" else {"signature"})
        with pytest.raises(SourceError, match="refused"):
            world["sources"].install(REGISTRY, "notes", approved_profile=report["profile"])
    assert world["lifecycle"].record("notes") is None and world["sources"].pin("notes") is None


def test_access_profiles_need_the_matching_administrator_approval(world):
    from core.module.manifest import ModuleManifest
    base = {"schema_version": "2.0", "name": "x", "version": "0.0.0.1", "capabilities": [], "kind": "application",
            "container": CONTAINER}
    assert access_profile(ModuleManifest.from_dict({**base, "permissions": []})) == "minimal"
    assert access_profile(ModuleManifest.from_dict({**base, "permissions": ["characters.write"]})) == "standard"
    assert access_profile(ModuleManifest.from_dict({**base, "permissions": ["publishing.publish"]})) == "elevated"
    assert access_profile(ModuleManifest.from_dict({**base, "permissions": ["unknown.thing"]})) == "elevated"
    publish_registry(world, "0.0.0.1", permissions=["publishing.publish"])
    assert world["sources"].assess(REGISTRY, "notes")["profile"] == "elevated"
    with pytest.raises(SourceError, match="elevated access profile"):
        world["sources"].install(REGISTRY, "notes", approved_profile="minimal")
    assert world["sources"].install(REGISTRY, "notes", approved_profile="elevated")["profile"] == "elevated"


def test_a_confirmed_critical_threat_blocks_the_module_and_a_fixed_version_lifts_it(world):
    publish_registry(world, "0.0.0.1")
    world["sources"].install(REGISTRY, "notes", approved_profile="minimal")
    world["lifecycle"].activate("notes")
    # An unconfirmed or non-critical advisory and a lost-support notice only warn.
    world["feed"]["document"]["advisories"] = [
        {"id": "VA-1", "module": "notes", "versions": ["0.0.0.1"], "severity": "critical", "confirmed": False},
        {"id": "VA-2", "module": "notes", "versions": "all", "severity": "low", "unsupported": True}]
    assert world["sources"].enforce() == [] and world["lifecycle"].record("notes")["state"] == "ACTIVE"
    assert world["sources"].assess(REGISTRY, "notes")["warnings"]  # unsupported: keeps working
    world["feed"]["document"]["advisories"].append(
        {"id": "VA-3", "module": "notes", "versions": ["0.0.0.1"], "severity": "critical", "confirmed": True})
    assert world["sources"].enforce() == [{"module_id": "notes", "version": "0.0.0.1", "advisory": "VA-3"}]
    record = world["lifecycle"].record("notes")
    assert record["state"] == "INACTIVE" and record["blocked"]["reason"] == "VA-3"
    with pytest.raises(LifecycleError, match="blocked"):
        world["lifecycle"].activate("notes")
    assert not world["sources"].assess(REGISTRY, "notes")["ok"]  # the affected version cannot be reinstalled
    publish_registry(world, "0.0.0.1", "0.0.0.2")
    world["sources"].update("notes", "0.0.0.2", approved_profile="minimal")
    assert "blocked" not in world["lifecycle"].record("notes")
    assert world["lifecycle"].activate("notes")["state"] == "ACTIVE"


def test_an_unsigned_or_altered_advisory_feed_is_refused(feed_key):
    signed = sign_advisories({"format_version": 1, "advisories": []}, feed_key[0])
    assert verify_advisories(signed, feed_key[1]) == []
    signed["advisories"] = [{"module": "notes", "versions": "all", "severity": "critical", "confirmed": True}]
    with pytest.raises(SourceError, match="signature"):
        verify_advisories(signed, feed_key[1])
    with pytest.raises(SourceError, match="signature"):
        verify_advisories({"format_version": 1, "advisories": []}, feed_key[1])


def test_the_core_cache_keeps_the_active_and_two_previous_packages(world):
    publish_registry(world, "0.0.0.1")
    world["sources"].install(REGISTRY, "notes", approved_profile="minimal")
    for version in ("0.0.0.2", "0.0.0.3", "0.0.0.4"):
        publish_registry(world, *[f"0.0.0.{n}" for n in range(1, int(version[-1]) + 1)])
        world["sources"].update("notes", version, approved_profile="minimal")
    kept = sorted(name.rsplit("-", 1)[0] for name in world["sources"].cache.entries("notes"))
    assert kept == ["0.0.0.2", "0.0.0.3", "0.0.0.4"]


def test_an_official_module_installed_with_the_release_updates_from_the_official_repository(tmp_path):
    """21.11.2026: release-installed modules had no pinned source, so the Updates panel said
    "Немає модулів із закріпленим джерелом" and they could never update apart from CORE."""
    from core import components as c
    signing = Ed25519PrivateKey.generate()
    private = signing.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    trust = TrustStore({"vertep": Publisher("vertep", {"vertep-modules-1": pem(signing)},
                                            ["github.com/fylypovych/vertep"])})
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda p: verify_package(p, trust), drain_timeout=0.1,
                                system_versions={"core_runtime": "0.0.4.27"})
    lifecycle.install_bundled("storyboard")  # no migrations: no database needed here
    installed = lifecycle.record("storyboard")["version"]

    unsigned = c.build_package("storyboard", image="ghcr.io/fylypovych/vertep-module-storyboard@sha256:" + "b" * 64)
    unsigned["manifest"] = {**unsigned["manifest"], "version": "0.0.9.1"}
    data = json.dumps(c.sign_package(unsigned, private, "vertep-modules-1")).encode()
    network = Network()
    asset = "https://github.com/fylypovych/vertep/releases/download/storyboard.0.0.9.1/package.json"
    network.files[asset] = data
    network.files["https://api.github.com/repos/fylypovych/vertep/releases?per_page=100"] = json.dumps([
        {"tag_name": "0.0.4.27", "draft": False, "prerelease": False, "assets": []},
        {"tag_name": "storyboard.0.0.9.1", "draft": False, "prerelease": False,
         "assets": [{"name": "package.json", "browser_download_url": asset}]}]).encode()
    sources = ModuleSources(lifecycle, SourceResolver(network), core_version="0.0.4.27")

    assert sources.pin("storyboard")["official"] is True
    assert sources.pin("text") is None  # role modules have no package of their own
    [offer] = [item for item in sources.recommendations() if item["module_id"] == "storyboard"]
    assert offer["current"] == installed and offer["available"] == "0.0.9.1" and offer["recommended"], offer
    result = sources.update("storyboard", "0.0.9.1", approved_profile=offer["profile"])
    assert result["version"] == "0.0.9.1" and lifecycle.record("storyboard")["version"] == "0.0.9.1"
    pin = sources.pin("storyboard")
    assert pin.get("official") is None and pin["version"] == "0.0.9.1" and pin["history"] == [installed]
