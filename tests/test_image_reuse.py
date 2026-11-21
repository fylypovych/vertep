"""Release reuses an image whose inputs did not change (owner decision 21.11.2026)."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_reuse", ROOT / "scripts" / "image-reuse.py")
reuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reuse)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "a.yml").write_text("a: 1\n")
    (tmp_path / "entry.sh").write_text("#!/bin/sh\n")
    (tmp_path / "unrelated.txt").write_text("x\n")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM nginx:1.27-alpine\nCOPY conf /etc/conf\nCOPY --chmod=755 entry.sh /entry.sh\n"
                          "COPY --from=build /out /srv\n")
    return tmp_path, dockerfile


def test_the_fingerprint_follows_exactly_the_copied_files_and_the_dockerfile(context):
    root, dockerfile = context
    first = reuse.fingerprint(dockerfile, root, "linux/amd64")
    (root / "unrelated.txt").write_text("changed\n")
    assert reuse.fingerprint(dockerfile, root, "linux/amd64") == first  # not copied: no rebuild
    (root / "conf" / "a.yml").write_text("a: 2\n")
    second = reuse.fingerprint(dockerfile, root, "linux/amd64")
    assert second != first
    assert reuse.fingerprint(dockerfile, root, "linux/amd64,linux/arm64") != second
    dockerfile.write_text(dockerfile.read_text().replace("1.27", "1.28"))
    assert reuse.fingerprint(dockerfile, root, "linux/amd64") != second


def _inspect(value, built_at):
    labels = {reuse.LABEL: value, reuse.BUILT_AT: built_at}
    return {"linux/amd64": {"config": {"Labels": labels}}, "linux/arm64": {"config": {"Labels": labels}}}


def test_reuse_only_for_the_same_inputs_within_the_age_limit(context):
    root, dockerfile = context
    value = reuse.fingerprint(dockerfile, root, "linux/amd64")
    fresh = (NOW - timedelta(days=2)).isoformat()
    assert reuse.decide(value, _inspect(value, fresh), 7, NOW, dockerfile)["reuse"] is True
    assert reuse.decide("other", _inspect(value, fresh), 7, NOW, dockerfile)["reuse"] is False
    old = (NOW - timedelta(days=9)).isoformat()
    assert "refresh" in reuse.decide(value, _inspect(value, old), 7, NOW, dockerfile)["reason"]
    assert reuse.decide(value, None, 7, NOW, dockerfile) == {"reuse": False, "reason": "no previous image"}
    # Images released before this feature have no label: rebuilt once, then reused.
    assert reuse.decide(value, {"config": {"Labels": {}}}, 7, NOW, dockerfile)["reuse"] is False


def test_images_that_bake_the_version_in_are_always_rebuilt():
    baking = [path for path in [ROOT / "Dockerfile", *sorted((ROOT / "docker").glob("*/Dockerfile"))]
              if reuse.bakes_version(path)]
    assert {path.relative_to(ROOT).as_posix() for path in baking} == {
        "Dockerfile", "docker/web-ui/Dockerfile", "docker/moneyprinter/Dockerfile"}
    assert reuse.decide("x", _inspect("x", NOW.isoformat()), 7, NOW, ROOT / "Dockerfile")["reuse"] is False


def test_every_release_dockerfile_has_a_computable_fingerprint():
    for dockerfile in [ROOT / "Dockerfile", *sorted((ROOT / "docker").glob("*/Dockerfile"))]:
        assert len(reuse.fingerprint(dockerfile, ROOT, "linux/amd64")) == 64, dockerfile
    for dockerfile in sorted((ROOT / "modules").glob("*/Dockerfile")):
        assert len(reuse.fingerprint(dockerfile, dockerfile.parent, "linux/amd64")) == 64, dockerfile


def test_the_release_builds_only_when_reuse_was_refused_and_labels_new_images():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    for job in ("build-images", "build-module-images"):
        steps = workflow["jobs"][job]["steps"]
        names = [step.get("id") for step in steps]
        assert names.index("reuse") < names.index("build"), job
        build = steps[names.index("build")]
        assert build["if"] == "steps.reuse.outputs.reuse != 'true'"
        assert "org.vertep.inputs=${{ steps.reuse.outputs.fingerprint }}" in build["with"]["labels"]
        script = "\n".join(step.get("run", "") for step in steps)
        assert "steps.build.outputs.digest || steps.reuse.outputs.digest" in script
        assert "imagetools create -t" in steps[names.index("reuse")]["run"]
