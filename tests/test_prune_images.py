"""#517: old local Vertep images are removed after a successful update.

The fake Docker below reproduces the stand: containerd snapshotter, `<none>` entries that
`dangling=true` would not report (it is never used), images of another project, images of
stopped containers.
"""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prune_images", ROOT / "scripts" / "prune-images.py")
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)

REPO = "ghcr.io/fylypovych/vertep-"


def digest(name: str) -> str:
    return "sha256:" + name.encode().hex().ljust(64, "0")[:64]


def env_for(version: str) -> str:
    return "".join(f"VERTEP_{service.upper()}_IMAGE={REPO}{service}:{version}@{digest(service + version)}\n"
                   for service in ("core", "proxy")) + "VERTEP_VERSION=" + version + "\n"


class Docker:
    def __init__(self, images, containers=()):
        self.images = {image["Id"]: image for image in images}
        self.containers = list(containers)
        self.removed = []

    def __call__(self, command, capture_output=True, text=True, check=False):
        args = command[1:]
        if args[:2] == ["ps", "-aq"]:
            out = "\n".join(f"c{index}" for index, _ in enumerate(self.containers))
        elif args[0] == "inspect":
            out = "\n".join(self.containers)
        elif args[:3] == ["image", "ls", "-aq"]:
            out = "\n".join(list(self.images) + list(self.images)[:1])  # containerd lists duplicates
        elif args[:2] == ["image", "inspect"]:
            out = json.dumps([self.images[item] for item in args[2:]])
        elif args[:2] == ["image", "rm"]:
            if args[2] in self.containers:
                return SimpleNamespace(returncode=1, stdout="", stderr="image is in use")
            self.removed.append(self.images.pop(args[2])["Id"])
            out = ""
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def vertep_image(service, version, tagged=True):
    return {"Id": digest(f"id-{service}-{version}"), "Size": 100_000_000,
            "RepoTags": [f"{REPO}{service}:{version}"] if tagged else [],
            "RepoDigests": [f"{REPO}{service}@{digest(service + version)}"],
            "Config": {"Labels": {prune.SOURCE_LABEL: prune.VERTEP_SOURCE}}}


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "opt-vertep"
    (root / "config").mkdir(parents=True)
    return root


def install(root, version):
    (root / ".env").write_text(env_for(version))
    prune.record(root, version)


def test_only_images_of_the_last_three_successful_versions_stay(host):
    versions = ["0.0.4.24", "0.0.4.25", "0.0.4.26", "0.0.4.27"]
    for version in versions:
        install(host, version)
    images = [vertep_image(service, version) for version in versions for service in ("core", "proxy")]
    untagged_old = vertep_image("proxy", "0.0.4.20", tagged=False)  # `<none>`, not dangling
    untagged_old["RepoDigests"] = []                                 # only the label proves ownership
    foreign = {"Id": digest("other"), "Size": 1, "RepoTags": ["postgres:16"], "RepoDigests": [],
               "Config": {"Labels": {}}}
    unknown_none = {"Id": digest("unknown"), "Size": 1, "RepoTags": [], "RepoDigests": [], "Config": {"Labels": {}}}
    stopped = vertep_image("core", "0.0.4.10")
    docker = Docker(images + [untagged_old, foreign, unknown_none, stopped], containers=[stopped["Id"]])

    report = prune.prune(host, keep=3, runner=docker)

    removed = set(docker.removed)
    assert removed == {vertep_image(s, "0.0.4.24")["Id"] for s in ("core", "proxy")} | {untagged_old["Id"]}
    assert report["kept_versions"] == ["0.0.4.25", "0.0.4.26", "0.0.4.27"]
    assert report["foreign"] == 2 and report["in_use"] == 1  # other project, unknown <none>, stopped container
    assert report["freed_bytes"] == 3 * 100_000_000


def test_a_dry_run_removes_nothing_and_a_failed_update_never_prunes(host):
    for version in ("0.0.4.20", "0.0.4.21", "0.0.4.22", "0.0.4.23"):
        install(host, version)
    docker = Docker([vertep_image("core", "0.0.4.20")])
    assert prune.prune(host, keep=3, dry_run=True, runner=docker)["removed"] == [f"{REPO}core:0.0.4.20"]
    assert docker.removed == []
    # The integration runs prune only on the success paths of apply-update and bootstrap.
    script = (ROOT / "scripts" / "vertep").read_text()
    failure = script.index('echo "Update health check failed; the update agent will rollback automatically."')
    assert script.rfind("prune_old_images", 0, failure) > script.index("for attempt in {1..120}; do", script.index("VERIFYING"))
    assert "prune_old_images" not in script[failure:failure + 200]


def test_before_three_versions_are_recorded_the_host_references_are_kept(host):
    (host / ".env").write_text(env_for("0.0.4.27"))
    prune.record(host, "0.0.4.27")
    snapshot = host / "backups" / "bootstrap-20261120-200000"
    snapshot.mkdir(parents=True)
    (snapshot / ".env").write_text(env_for("0.0.4.23"))  # earlier version, rollback snapshot
    docker = Docker([vertep_image("core", "0.0.4.23"), vertep_image("core", "0.0.4.27"),
                     vertep_image("core", "0.0.4.10")])
    prune.prune(host, keep=3, runner=docker)
    assert docker.removed == [vertep_image("core", "0.0.4.10")["Id"]]


def test_a_shared_image_with_a_foreign_tag_is_never_removed(host):
    for version in ("0.0.4.20", "0.0.4.21", "0.0.4.22", "0.0.4.23"):
        install(host, version)
    shared = vertep_image("core", "0.0.4.20")
    shared["RepoTags"].append("mycompany/base:latest")
    docker = Docker([shared])
    assert prune.prune(host, keep=3, runner=docker)["foreign"] == 1 and docker.removed == []


def test_the_cleanup_ships_with_the_release_and_runs_after_a_healthy_bootstrap():
    bundle = (ROOT / "scripts" / "build-runtime-bundle.py").read_text()
    assert '"prune-images.py"' in bundle
    bootstrap = (ROOT / "bootstrap.sh").read_text()
    healthy = bootstrap.index('progress "Waiting for runtime health checks"')
    assert bootstrap.index("prune-images.py\" prune", healthy) > healthy
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "vertep")]).returncode == 0


def _docker_available():
    import shutil
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="needs a Docker daemon")
def test_real_containerd_store_removes_old_and_untagged_vertep_images_and_keeps_rollback(host, tmp_path):
    """Real Docker (containerd snapshotter on the dev host): `<none>` Vertep images that
    `dangling=true` does not report are removed; the previous kept version stays usable."""
    import uuid
    repo = f"vertep-prune-{uuid.uuid4().hex[:8]}"
    built = []

    def build(tag, content):
        context = tmp_path / f"ctx-{content}"
        context.mkdir(exist_ok=True)
        (context / "v.txt").write_text(content)
        (context / "Dockerfile").write_text(
            f'FROM scratch\nCOPY v.txt /v.txt\nLABEL {prune.SOURCE_LABEL}="{prune.VERTEP_SOURCE}"\n')
        subprocess.run(["docker", "build", "-q", "-t", tag, str(context)], check=True, capture_output=True)
        built.append(tag)

    try:
        # Rebuilding a tag leaves the previous image without a tag: a `<none>` Vertep image.
        build(f"{repo}/proxy:0.0.0.9", "orphan-old")
        orphan = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", f"{repo}/proxy:0.0.0.9"],
                                capture_output=True, text=True, check=True).stdout.strip()
        build(f"{repo}/proxy:0.0.0.9", "orphan-new")
        subprocess.run(["docker", "image", "rm", f"{repo}/proxy:0.0.0.9"], capture_output=True)
        versions = ["0.0.1.1", "0.0.1.2", "0.0.1.3", "0.0.1.4"]
        for version in versions:
            build(f"{repo}/core:{version}", version)
            (host / ".env").write_text(f"VERTEP_IMAGE_REPOSITORY={repo}\nVERTEP_CORE_IMAGE={repo}/core:{version}\n")
            prune.record(host, version)
        dangling = subprocess.run(["docker", "images", "-q", "--filter", "dangling=true", "--filter",
                                   f"reference={repo}/*"], capture_output=True, text=True).stdout.split()

        report = prune.prune(host, keep=3)

        def present(reference):
            return subprocess.run(["docker", "image", "inspect", reference], capture_output=True).returncode == 0
        assert not present(f"{repo}/core:0.0.1.1"), report
        assert all(present(f"{repo}/core:{version}") for version in versions[1:]), report
        # Rollback to the previous kept version: its image is still local and runnable as a source.
        assert subprocess.run(["docker", "create", "--name", f"{repo}-rollback", f"{repo}/core:0.0.1.3", "x"],
                              capture_output=True).returncode == 0
        assert not present(orphan), report  # removed whatever `dangling=true` reported
        print(f"dangling=true listed {len(dangling)} image(s) of {repo}")
    finally:
        subprocess.run(["docker", "rm", "-f", f"{repo}-rollback"], capture_output=True)
        for tag in built:
            subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)
