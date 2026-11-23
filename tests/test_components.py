"""#510 (VAR #509): component versions, signed component releases and change-impact CI."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from core import components as c

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = c.load_registry()
MODULES = sorted(name for name, spec in REGISTRY["components"].items() if spec["kind"] == "module")


# -- change impact (CI rule) --------------------------------------------------------------

def test_an_internal_publisher_change_runs_its_tests_compatibility_and_lifecycle_only():
    result = c.impact(["publishers/telegram/__init__.py", "modules/publisher/publisher_module.py"])
    assert result.full_suite is False and result.components == ["publisher"]
    assert set(REGISTRY["components"]["publisher"]["tests"]) <= set(result.tests)
    # compatibility with supported CORE + install/update/rollback lifecycle
    assert {"tests/test_module_contract.py", "tests/test_module_lifecycle.py",
            "tests/test_role_module_migration.py"} <= set(result.tests)
    other_module_tests = {t for m in MODULES if m != "publisher" for t in REGISTRY["components"][m]["tests"]}
    assert not other_module_tests & set(result.tests)  # untouched modules are skipped


def test_a_shared_contract_change_checks_every_dependent_component():
    result = c.impact(["core/module/manifest.py"])
    assert result.components == MODULES and result.full_suite is False
    assert "core/module/manifest.py" in result.reasons["shared_contract"]
    for module in MODULES:
        assert set(REGISTRY["components"][module]["tests"]) <= set(result.tests)


@pytest.mark.parametrize("path", ["core/pipeline.py", "something/unknown.py", "tests/test_not_owned_anywhere.py",
                                  "requirements.txt", ".github/workflows/ci.yml"])
def test_core_or_unknown_impact_runs_the_full_suite(path):
    result = c.impact([path])
    assert result.full_suite is True and result.tests == []


def test_docs_only_changes_need_no_component_tests():
    result = c.impact(["CHANGELOG.md", "releases/0.0.4.20.md", "README.md"])
    assert result.to_dict() == {"full_suite": False, "components": [], "tests": [], "reasons": {}}


def test_a_component_test_change_runs_that_component():
    result = c.impact(["tests/test_brands_module.py"])
    assert result.components == ["brands"] and not result.full_suite


# -- numbering ------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("0.0.1.30", "0.0.1.31"), ("0.0.1.99", "0.0.2.0"),
                                            ("0.0.99.99", "0.1.0.0"), ("0.99.99.99", "1.0.0.0"),
                                            ("0.0.0.0", "0.0.0.1")])
def test_digits_carry_over_after_99(value, expected):
    assert c.next_version(value) == expected


@pytest.mark.parametrize("value", ["1.0.0", "0.0.1.100", "v0.0.1.1", "0.0.1", "a.b.c.d", ""])
def test_only_a_b_c_d_with_digits_up_to_99_is_a_version(value):
    with pytest.raises(c.ComponentError):
        c.parse_version(value)


def test_tags_are_component_dot_version_and_historical_tags_are_kept_apart():
    assert c.tag_name("web-ui", "0.0.4.19") == "web-ui.0.0.4.19"
    assert c.parse_tag("web-ui.0.0.4.19") == ("web-ui", "0.0.4.19")
    assert c.parse_tag("0.0.4.17") is None  # historical shared runtime tag
    tags = ["0.0.4.17", "brands.0.0.4.15", "brands.0.0.4.16", "characters.0.0.4.15"]
    assert c.component_versions("brands", tags) == ["0.0.4.15", "0.0.4.16"]


def test_collisions_and_non_forward_versions_are_refused():
    tags = {"brands.0.0.4.15": "a" * 40, "0.0.4.19": "b" * 40}
    assert c.check_release("brands", "0.0.4.15", "a" * 40, tags) == "brands.0.0.4.15"  # same commit: idempotent
    with pytest.raises(c.ComponentError, match="already points"):
        c.check_release("brands", "0.0.4.15", "c" * 40, tags)
    with pytest.raises(c.ComponentError, match="not newer"):
        c.check_release("brands", "0.0.4.14", "c" * 40, tags)
    assert c.check_release("brands", "0.0.4.16", "c" * 40, tags) == "brands.0.0.4.16"
    # The shared tag 0.0.4.19 does not collide with a component of the same number.
    assert c.check_release("publisher", "0.0.4.19", "c" * 40, tags) == "publisher.0.0.4.19"


def test_transition_numbers_continue_history_and_new_modules_start_at_0_0_0_1():
    assert c.transition_version("brands", "0.0.4.14", []) == "0.0.4.15"
    assert c.transition_version("brands", "0.0.4.14", ["brands.0.0.4.15"]) == "0.0.4.16"
    assert c.transition_version("text", "0.0.2.99", []) == "0.0.3.0"
    assert c.transition_version("newmodule", None, []) == "0.0.0.1"


def test_shared_history_maps_misnamed_commits_and_finds_the_last_change():
    # CHANGELOG (newest first) is the authority; one original subject even repeats a version.
    changelog = ("## ПРАВИЛЬНА НАЗВА: 0.0.1.4\n## ОРИГІНАЛЬНА НАЗВА: 0.0.1.1\n- y\n\n"
                 "## ПРАВИЛЬНА НАЗВА: 0.0.1.3\n- z\n\n"
                 "## ПРАВИЛЬНА НАЗВА: 0.0.1.2\n## ОРИГІНАЛЬНА НАЗВА: fix publisher\n- x\n\n"
                 "## ПРАВИЛЬНА НАЗВА: 0.0.1.1\n## ОРИГІНАЛЬНА НАЗВА: 0.0.1.1 — початок\n- w\n")
    log = [("1" * 40, "0.0.1.1 — початок", ["modules/brands/a.py"]), ("2" * 40, "work in progress", ["modules/publisher/x.py"]),
           ("3" * 40, "fix publisher", ["core/a.py"]), ("4" * 40, "0.0.1.3", ["modules/brands/b.py"]),
           ("5" * 40, "0.0.1.1", ["modules/storyboard/c.py"])]
    releases = c.shared_releases(log, changelog)
    assert [version for version, _ in releases] == ["0.0.1.1", "0.0.1.2", "0.0.1.3", "0.0.1.4"]
    assert c.last_shared_change("storyboard", releases) == "0.0.1.4"  # the repeated subject is 0.0.1.4
    assert c.last_shared_change("publisher", releases) == "0.0.1.2"  # its change shipped in 0.0.1.2
    assert c.last_shared_change("brands", releases) == "0.0.1.3"


def test_every_module_declares_its_transition_version_and_legacy_values_rank_below_it():
    module_dirs = {path.parent.name for path in (ROOT / "modules").glob("*/manifest.yaml")}
    # Every official module is a registered component; the LAB keeps its manifest beside its deployment.
    assert module_dirs | {"lab"} == set(MODULES)
    assert REGISTRY["components"]["lab"]["version_source"] == "deploy/lab/manifest.yaml"
    for module in MODULES:
        spec = REGISTRY["components"][module]
        declared = c.declared_version(module)
        # The first independent release follows the transition base; later releases move forward.
        assert c.parse_version(declared) >= c.parse_version(c.next_version(spec["transition_base"])), module
        for legacy, base in spec.get("legacy_versions", {}).items():
            assert c.version_key(module, legacy) < c.version_key(module, declared)
    assert c.declared_version("core") == (ROOT / "VERSION").read_text().strip()  # CORE keeps VERSION


def test_registry_picks_the_independent_release_over_a_legacy_instance():
    from packaging.version import Version
    from core.module.instances import _ordering
    assert _ordering("brands", "1.0.0", Version) < _ordering("brands", "0.0.4.15", Version)


def test_shared_release_numbering_ignores_component_tags():
    spec = importlib.util.spec_from_file_location("release_script", ROOT / "scripts" / "release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    assert release.next_version(["brands.0.0.9.99", "0.0.4.19"]) == "0.0.4.20"


# -- signed component manifest ---------------------------------------------------------------

@pytest.fixture(scope="module")
def keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public


def _manifest(keys, **override):
    import hashlib
    document = c.build_manifest("publisher", "0.0.3.91", "f" * 40,
                                [{"name": "package.json", "sha256": hashlib.sha256(b"pkg").hexdigest(), "size": 3},
                                 {"name": "image", "digest": "sha256:" + "a" * 64}],
                                compatibility={"core": {"min": "0.0.3.95", "max": "0.0.9.99"}, "api": None},
                                dependencies=[])
    document.update(override)
    return c.sign_manifest(document, keys[0])


def test_a_signed_stable_manifest_verifies_with_identity_compatibility_and_hashes(keys):
    signed = _manifest(keys)
    verified = c.verify_manifest(signed, keys[1], artifacts={"package.json": b"pkg"})
    assert verified["tag"] == "publisher.0.0.3.91" and verified["source"]["repository"] == "fylypovych/vertep"
    assert verified["compatibility"]["core"]["min"] == "0.0.3.95" and verified["format_version"] == 1


@pytest.mark.parametrize("tamper,error", [
    (lambda m: m.__setitem__("version", "0.0.3.92"), "signature"),
    (lambda m: None, "hash"),
])
def test_tampered_manifests_or_artifacts_are_refused(keys, tamper, error):
    signed = _manifest(keys)
    tamper(signed)
    with pytest.raises(c.ComponentError, match=error):
        c.verify_manifest(signed, keys[1], artifacts={"package.json": b"other bytes"})


@pytest.mark.parametrize("override,error", [({"channel": "beta"}, "stable"),
                                            ({"source": {"repository": "someone/fork", "commit": "f" * 40}}, "Vertep repository"),
                                            ({"tag": "publisher.9.9.9.9"}, "Tag")])
def test_unstable_foreign_or_mislabelled_releases_are_refused(keys, override, error):
    with pytest.raises(c.ComponentError, match=error):
        c.verify_manifest(_manifest(keys, **override), keys[1])


@pytest.mark.parametrize("component", ["brands", "publisher"])
def test_the_manifest_cli_signs_a_real_module_release(keys, tmp_path, component):
    import subprocess
    import sys
    private = tmp_path / "key.pem"
    private.write_bytes(keys[0])
    package = tmp_path / "package.json"
    package.write_bytes(b"{}")
    out = tmp_path / "manifest.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "component-release.py"), "manifest", "--component", component,
                    "--sha", "e" * 40, "--private-key", str(private), "--output", str(out),
                    "--artifact", f"package.json={package}", "--image", "sha256:" + "b" * 64],
                   check=True, capture_output=True, text=True)
    manifest = json.loads(out.read_text())
    c.verify_manifest(manifest, keys[1], artifacts={"package.json": b"{}"})
    assert manifest["tag"] == f"{component}.{c.declared_version(component)}"
    # Publisher's bundled role manifest has no compatibility; the package manifest has it (#510).
    assert manifest["compatibility"]["api"] == {"namespace": component, "version": "1.0"}
    assert manifest["compatibility"]["core"]["min"] and manifest["compatibility"]["core"]["max"]


# -- CI workflow ---------------------------------------------------------------------------------

def test_the_component_workflow_publishes_only_after_tests_and_never_as_latest():
    workflow = yaml.safe_load((ROOT / ".github/workflows/component-release.yml").read_text())
    jobs = workflow["jobs"]
    assert set(jobs["publish"]["needs"]) == {"impact", "tests"}
    assert "COMPONENT_RELEASES_ENABLED" in jobs["publish"]["if"]  # off until the owner enables it
    assert jobs["tests"]["needs"] == "impact"
    script = "\n".join(step.get("run", "") for step in jobs["publish"]["steps"])
    assert "--latest=false" in script and "plan" in script and "--verify-tag" in script
    # An annotated tag on a runner needs a committer identity before the first "git tag -a" (#510).
    assert script.index("git config user.email") < script.index("git tag -a")


def test_the_transition_record_matches_the_registry_and_the_manifests():
    transition = json.loads((ROOT / "config" / "component-transition.json").read_text())
    # The LAB joined the component releases after the #509 transition (#523, base from history).
    assert set(transition["modules"]) == set(MODULES) - {"lab"}
    for module, entry in transition["modules"].items():
        spec = REGISTRY["components"][module]
        assert entry["base"] == spec["transition_base"], module
        assert entry["first_independent"] == c.next_version(entry["base"]), module
        # Later component releases move the manifest forward, never behind the first independent one.
        assert c.parse_version(c.declared_version(module)) >= c.parse_version(entry["first_independent"]), module
        assert entry["digest"].startswith("sha256:") and len(entry["digest"]) == 71, module
        assert c.parse_version(entry["published_release"]) >= c.parse_version(entry["base"])
    assert transition["core"]["first_independent"] == c.next_version(transition["core"]["base"])


def test_the_full_history_reconciles_with_the_changelog_count_of_issue_509():
    import subprocess
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT, capture_output=True, text=True)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        pytest.skip("needs the full Git history")
    raw = subprocess.run(["git", "log", "--reverse", "--format=@@%H%x09%s", "--name-only"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    log = []
    for block in raw.split("@@")[1:]:
        head, *files = block.strip().splitlines()
        sha, subject = head.split("\t", 1)
        log.append((sha, subject, [f for f in files if f]))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = c.shared_releases(log, changelog)
    up_to = [v for v, _ in releases if c.parse_version(v) <= c.parse_version("0.0.4.16")]
    transition = json.loads((ROOT / "config" / "component-transition.json").read_text())["history"]
    assert len(up_to) == transition["version_commits_matched_up_to_0_0_4_16"] == 415
    assert len(up_to) + len(transition["changelog_entries_without_a_main_commit"]) == 416  # the #509 count
    # The bases are a snapshot at the transition; later releases (this very renumbering) do not move them.
    snapshot = [(v, files) for v, files in releases
                if c.parse_version(v) <= c.parse_version(transition["last_shared_version"])]
    for module in set(MODULES) - {"lab"}:
        assert c.last_shared_change(module, snapshot) == REGISTRY["components"][module]["transition_base"], module


# -- module packages (#510/#511, 20.11.2026) ------------------------------------------------------

def _ed25519_trust(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from core.module.trust import TrustStore
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    (tmp_path / "publishers.json").write_text(json.dumps(
        {"vertep": {"keys": {"test-key": public}, "repositories": ["github.com/fylypovych/vertep"]}}))
    return private, TrustStore.from_root(tmp_path)


def test_every_module_with_a_container_gets_a_signed_package_core_accepts(tmp_path):
    from core.module.packages import verify_package
    private, trust = _ed25519_trust(tmp_path)
    for module, spec in REGISTRY["components"].items():
        if not spec.get("image"):
            continue
        image = f"ghcr.io/fylypovych/{spec['image']['name']}@sha256:" + "a" * 64
        extra = {item["service"]: f"ghcr.io/fylypovych/{item['name']}@sha256:" + "b" * 64
                 for item in spec.get("extra_images", [])}
        package = c.sign_package(c.build_package(module, image=image, extra_images=extra), private, "test-key")
        manifest = verify_package(package, trust)
        assert manifest.version == c.declared_version(module)
        assert manifest.to_dict()["container"]["image"] == image
        if manifest.ui and manifest.ui.get("files"):
            assert "index.html" in package["files"]  # the UI bundle travels with its signed digest


def test_a_role_module_packages_its_container_manifest_with_the_same_version():
    """Publisher (21.11.2026): the release keeps the role manifest, the package the container one."""
    import yaml
    spec = REGISTRY["components"]["publisher"]
    app = yaml.safe_load((ROOT / spec["package_manifest"]).read_text(encoding="utf-8"))
    assert app["version"] == c.declared_version("publisher") and app["kind"] == "application"
    assert spec["image"]["file"] == "modules/publisher/app/Dockerfile" and spec["image"]["context"] == "."


def test_a_package_refuses_an_unpinned_image_and_a_tampered_file(tmp_path):
    from core.module.packages import verify_package
    from core.module.trust import TrustError
    private, trust = _ed25519_trust(tmp_path)
    with pytest.raises(c.ComponentError):
        c.build_package("monitoring", image="ghcr.io/fylypovych/vertep-module-monitoring:latest")
    package = c.sign_package(c.build_package("monitoring"), private, "test-key")
    package["files"]["index.html"] = package["files"]["index.html"][:-4] + "AAAA"
    with pytest.raises(TrustError):
        verify_package(package, trust)


def test_the_official_publisher_key_is_trusted_for_the_vertep_repository():
    data = json.loads((ROOT / "config" / "module-trust" / "publishers.json").read_text())
    assert data["vertep"]["repositories"] == ["github.com/fylypovych/vertep"]
    assert all("BEGIN PUBLIC KEY" in pem for pem in data["vertep"]["keys"].values())


def test_the_publish_job_builds_images_and_uploads_signed_packages():
    workflow = yaml.safe_load((ROOT / ".github/workflows/component-release.yml").read_text())
    publish = workflow["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write", "packages": "write"}
    script = "\n".join(step.get("run", "") for step in publish["steps"])
    assert "MODULE_SIGNING_KEY" in script and "component-release.py package" in script
    assert "package.json.sha256" in script and "--push" in script
    # Releases published before packages existed get them added, never re-tagged.
    assert "gh release upload" in script and "--clobber" in script and "--releases releases.json" in script


def test_plan_names_the_tag_of_a_release_that_only_gets_its_package(tmp_path, monkeypatch):
    """Run 44890975001: a backfill entry had no "tag", so the package went to release ""."""
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location("component_release", ROOT / "scripts" / "component-release.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    tag = "brands." + c.declared_version("brands")  # the current version of the module
    monkeypatch.setattr(cli, "_tags", lambda: {tag: "a" * 40})
    monkeypatch.setattr(cli, "_git", lambda *args: "")  # no brands file changed since its tag
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([{"tag": tag, "assets": [tag + ".manifest.json"]}]))
    plan = cli.cmd_plan(types.SimpleNamespace(components="brands", sha="b" * 40, releases=str(releases)))
    entry = plan["brands"]
    assert entry["backfill"] and not entry["publish"] and "error" not in entry
    assert entry["tag"] == tag and entry["commit"] == "a" * 40


# -- the LAB as a signed component (#491 L5, #523) -------------------------------------------------

LAB_CONTROL = "ghcr.io/fylypovych/vertep-lab-control@sha256:" + "c" * 64
LAB_WEB = "ghcr.io/fylypovych/vertep-lab-web@sha256:" + "d" * 64


def _lab_package(private):
    return c.sign_package(c.build_package("lab", image=LAB_CONTROL, extra_images={"lab-web": LAB_WEB}),
                          private, "test-key")


def test_the_component_plan_includes_the_lab_with_both_images(monkeypatch):
    import types
    spec = importlib.util.spec_from_file_location("component_release_lab", ROOT / "scripts" / "component-release.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "_tags", lambda: {})
    assert "lab" in c.impact(["lab/api.py"]).components
    assert "lab" in c.impact(["bootstrap-lab.sh", "deploy/lab/web/nginx.conf"]).components
    entry = cli.cmd_plan(types.SimpleNamespace(components="lab", sha="f" * 40, releases=None))["lab"]
    assert entry["publish"] and entry["tag"] == f"lab.{c.declared_version('lab')}"
    assert entry["image"]["name"] == "vertep-lab-control"
    assert [item["name"] for item in entry["extra_images"]] == ["vertep-lab-web"]


def test_a_signed_lab_package_verifies_and_installs_only_as_isolated_lab(tmp_path):
    import base64
    from core.module.lifecycle import ModuleLifecycle
    from core.module.packages import verify_package
    private, trust = _ed25519_trust(tmp_path)
    package = _lab_package(private)
    manifest = verify_package(package, trust)
    assert manifest.name == "lab" and manifest.placement["target"] == "isolated_lab"
    assert manifest.to_dict()["container"]["image"] == LAB_CONTROL
    assert json.loads(base64.b64decode(package["files"]["images.json"])) == {"lab-control": LAB_CONTROL,
                                                                            "lab-web": LAB_WEB}
    compose = base64.b64decode(package["files"]["docker-compose.yml"])
    assert compose == (ROOT / "deploy/lab/docker-compose.web.yml").read_bytes()
    lifecycle = ModuleLifecycle(tmp_path / "lc", lambda p: verify_package(p, trust),
                                system_versions={"core_runtime": (ROOT / "VERSION").read_text().strip()})
    lifecycle.install(package)
    record = lifecycle.record("lab")
    assert record["state"] == "INSTALLED" and record["version"] == c.declared_version("lab")
    # Every image must be pinned by digest.
    with pytest.raises(c.ComponentError):
        c.build_package("lab", image=LAB_CONTROL, extra_images={"lab-web": "ghcr.io/x/lab-web:latest"})


@pytest.mark.parametrize("tamper", ["compose", "images", "signer"])
def test_a_forged_lab_package_is_refused(tmp_path, tamper):
    import base64
    from core.module.packages import verify_package
    from core.module.trust import TrustError
    private, trust = _ed25519_trust(tmp_path)
    package = _lab_package(private)
    if tamper == "compose":
        package["files"]["docker-compose.yml"] = base64.b64encode(b"services: {evil: {image: x}}").decode()
    elif tamper == "images":
        package["files"]["images.json"] = base64.b64encode(json.dumps(
            {"lab-control": LAB_CONTROL, "lab-web": "ghcr.io/evil/web@sha256:" + "e" * 64}).encode()).decode()
    else:
        package["signer"] = "unknown-key"
    with pytest.raises((TrustError, KeyError, ValueError)):
        verify_package(package, trust)


def _bootstrap_function(name: str) -> str:
    text = (ROOT / "bootstrap-lab.sh").read_text(encoding="utf-8")
    start = text.index(f"{name}(){{")
    return text[start:text.index("\n}\n", start)]


def test_bootstrap_lab_installs_the_verified_release_without_building():
    text = (ROOT / "bootstrap-lab.sh").read_text(encoding="utf-8")
    release = _bootstrap_function("install_from_release")
    assert "--build" not in release.replace("--no-build", "") and "git clone" not in release
    assert "docker-compose.web.build.yml" not in release
    # sha256 and the publisher signature are checked before compose pulls or starts anything.
    checks = [release.index("sha256sum -c"), release.index("key.verify("), release.index("publishers.json")]
    assert max(checks) < release.index("docker compose")
    assert 'manifest["placement"]["target"] == "isolated_lab"' in release
    # Building from source is only the explicit development path.
    assert "git clone" in _bootstrap_function("build_from_source")
    assert "if [[ $BUILD_FROM_SOURCE == 1 ]]; then build_from_source; else install_from_release; fi" in text
    assert "BUILD_FROM_SOURCE=${VERTEP_LAB_BUILD_FROM_SOURCE:-0}" in text


def test_bootstrap_lab_verifier_accepts_a_signed_package_and_rejects_a_forged_one(tmp_path):
    """Runs the exact Python verifier from bootstrap-lab.sh against real signed packages."""
    import re
    import subprocess
    import sys
    release = _bootstrap_function("install_from_release")
    verifier = re.search(r"<<'PY' \\\n.*?\n(.*?)\nPY\n", release, re.S).group(1)
    script = tmp_path / "verify.py"
    script.write_text(verifier)
    private, _trust = _ed25519_trust(tmp_path)
    package = _lab_package(private)
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(package))
    forged = json.loads(json.dumps(package))
    forged["manifest"]["placement"] = {**forged["manifest"]["placement"], "target": "any"}
    bad.write_text(json.dumps(forged))
    for name, expected_ok in (("good", True), ("bad", False)):
        root = tmp_path / f"root-{name}"
        root.mkdir()
        result = subprocess.run([sys.executable, "-I", str(script), str(tmp_path / f"{name}.json"),
                                 str(tmp_path / "publishers.json"), str(root)], capture_output=True, text=True)
        assert (result.returncode == 0) is expected_ok, result.stderr
        assert (root / "docker-compose.yml").exists() is expected_ok
        if expected_ok:
            assert f"LAB_CONTROL_IMAGE={LAB_CONTROL}" in result.stdout and f"LAB_WEB_IMAGE={LAB_WEB}" in result.stdout
            assert (root / "docker-compose.yml").read_bytes() == (ROOT / "deploy/lab/docker-compose.web.yml").read_bytes()
