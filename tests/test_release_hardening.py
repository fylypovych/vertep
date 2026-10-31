"""Regression tests for the release/update hardening (bootstrap, release.py, update agent)."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

POSIX_BASH = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None, reason="needs POSIX bash")


def load_script(name: str, module_name: str):
    path = Path("scripts", name).resolve()
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── release.py: secret scan ─────────────────────────────────────────────

PUBLIC_PEM = "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----\n"


def scan(monkeypatch, files: dict[str, str]) -> None:
    release = load_script("release.py", "vertep_release_hardening")
    monkeypatch.setattr(release, "changed_files", lambda _root: [("A", name) for name in files])
    monkeypatch.setattr(release, "git", lambda _root, *args: files[args[1][1:]])
    release.scan_staged_secrets(Path("."))


def test_public_pem_key_can_be_committed(monkeypatch):
    scan(monkeypatch, {"installer/update-public.pem": PUBLIC_PEM,
                       "installer/keys/release-2.pem": PUBLIC_PEM})


# Headers are assembled at runtime so this test file itself never matches the
# release secret scanner it exercises.
@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "ENCRYPTED ", "OPENSSH "])
def test_private_keys_are_rejected_in_any_file(monkeypatch, kind):
    header = "-----BEGIN " + kind + "PRIVATE" + " KEY-----"
    with pytest.raises(RuntimeError, match="Реліз заблоковано"):
        scan(monkeypatch, {"notes/key.txt": f"{header}\nabc\n"})
    with pytest.raises(RuntimeError, match="Реліз заблоковано"):
        scan(monkeypatch, {"installer/leak.pem": f"{PUBLIC_PEM}{header}\nabc\n"})


def test_pem_without_public_marker_and_pkcs12_are_rejected(monkeypatch):
    with pytest.raises(RuntimeError, match="Реліз заблоковано"):
        scan(monkeypatch, {"installer/unknown.pem": "just some bytes\n"})
    with pytest.raises(RuntimeError, match="Реліз заблоковано"):
        scan(monkeypatch, {"installer/bundle.p12": "x"})


# ── release.py: workflow dispatch bookkeeping ───────────────────────────

def fake_time(step: float = 100.0):
    clock = {"now": 0.0}
    return clock, types.SimpleNamespace(
        time=lambda: clock["now"],
        sleep=lambda _seconds: clock.__setitem__("now", clock["now"] + step))


def test_trigger_returns_the_run_created_by_this_dispatch(monkeypatch):
    release = load_script("release.py", "vertep_release_hardening")
    listings = iter([[{"databaseId": 7}], [{"databaseId": 7}],
                     [{"databaseId": 7}, {"databaseId": 9}]])
    dispatched = []
    monkeypatch.setattr(release, "gh_run_json", lambda _root, _args: next(listings))
    monkeypatch.setattr(release, "gh_run", lambda _root, args: dispatched.append(args) or "")
    monkeypatch.setattr(release, "time", types.SimpleNamespace(time=release.time.time,
                                                              sleep=lambda _s: None))

    run_id = release.GitHubActionsTrigger().run(Path("."), workflow="Vertep Release", sha="x")

    assert run_id == "9"
    assert dispatched == [["workflow", "run", "Vertep Release", "--ref", "main"]]


def test_trigger_fails_when_no_new_run_appears(monkeypatch):
    release = load_script("release.py", "vertep_release_hardening")
    _, clock_module = fake_time()
    monkeypatch.setattr(release, "time", clock_module)
    monkeypatch.setattr(release, "gh_run_json", lambda *_a: [{"databaseId": 7}])
    monkeypatch.setattr(release, "gh_run", lambda *_a: "")
    with pytest.raises(RuntimeError, match="run_id"):
        release.GitHubActionsTrigger().run(Path("."), workflow="Vertep Release", sha="x")


def test_release_reports_a_workflow_that_never_finishes(tmp_path, monkeypatch):
    release = load_script("release.py", "vertep_release_hardening")
    version = "0.0.0.21"
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## ПРАВИЛЬНА НАЗВА: {version}\n- Готово.\n", encoding="utf-8")
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / f"{version}.md").write_text(
        f"# Vertep {version}\n\n- Готово.\n", encoding="utf-8")
    monkeypatch.setattr(release, "git", lambda _r, *args: (
        version if args[:1] == ("log",) else "abc" * 13 if args[:1] == ("rev-parse",) else ""))
    _, clock_module = fake_time()
    monkeypatch.setattr(release, "time", clock_module)

    class Stuck:
        def run(self, root, *, workflow, sha):
            return "1"

        def wait(self, root, *, run_id, timeout):
            return {}

        def verify(self, root, *, version, expected_sha):
            raise AssertionError("verify must not run for an unfinished workflow")

    with pytest.raises(RuntimeError, match="не завершився"):
        release.orchestrate_release(tmp_path, skip_tests=True, timeout=1, gate_timeout=0,
                                    trigger=Stuck())


# ── runtime bundle: manifest lifetime ───────────────────────────────────

def test_manifest_validity_days_is_configurable_and_bounded(monkeypatch):
    bundle = load_script("build-runtime-bundle.py", "vertep_bundle_hardening")
    monkeypatch.delenv("RUNTIME_MANIFEST_VALIDITY_DAYS", raising=False)
    assert bundle.manifest_validity_days() == 365
    monkeypatch.setenv("RUNTIME_MANIFEST_VALIDITY_DAYS", "")  # unset repository variable
    assert bundle.manifest_validity_days() == 365
    monkeypatch.setenv("RUNTIME_MANIFEST_VALIDITY_DAYS", "90")
    assert bundle.manifest_validity_days() == 90
    for invalid in ("7", "1000"):
        monkeypatch.setenv("RUNTIME_MANIFEST_VALIDITY_DAYS", invalid)
        with pytest.raises(ValueError, match="between 30 and 730"):
            bundle.manifest_validity_days()


# ── update agent: audited replay reset ──────────────────────────────────

def test_reset_replay_state_removes_sequence_and_audits_reason(tmp_path):
    agent = load_script("update-agent.py", "vertep_update_agent_hardening")
    (tmp_path / "trusted-release.json").write_text(
        json.dumps({"release_sequence": 20296, "version": "0.0.2.96"}), encoding="utf-8")

    agent.reset_replay_state(tmp_path, "release 0.0.2.96 was withdrawn")

    assert not (tmp_path / "trusted-release.json").exists()
    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["action"] == "reset-replay-state"
    assert "release 0.0.2.96 was withdrawn" in entry["message"]
    assert "0.0.2.96" in entry["message"] and "20296" in entry["message"]
    assert entry["event_hash"]


def test_reset_replay_state_cli_requires_a_reason(tmp_path, monkeypatch):
    agent = load_script("update-agent.py", "vertep_update_agent_hardening")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "vertep").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "trusted-release.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["update-agent.py", "--root", str(tmp_path),
                                      "--state-dir", str(state), "--reset-replay-state",
                                      "--reason", "short"])
    with pytest.raises(SystemExit, match="--reason"):
        agent.main()
    assert (state / "trusted-release.json").exists()


def test_replay_error_points_to_the_audited_reset_command():
    from core.update_protocol import validate_replay_state
    manifest = {"release_sequence": 1, "version": "0.0.2.90", "sha256": "a" * 64}
    trusted = {"release_sequence": 2, "version": "0.0.2.91", "sha256": "b" * 64, "key_id": None}
    with pytest.raises(RuntimeError, match="replayed.*--reset-replay-state"):
        validate_replay_state(manifest, trusted)


# ── update protocol: rate limits ────────────────────────────────────────

def test_rate_limit_is_not_retried_but_server_errors_are(monkeypatch):
    from core import update_protocol
    monkeypatch.setenv("UPDATE_HTTP_RETRIES", "3")
    monkeypatch.setenv("UPDATE_HTTP_RETRY_DELAY_SECONDS", "0")
    calls = []

    def failing(code):
        def opener(request, **_kwargs):
            calls.append(code)
            raise urllib.error.HTTPError(request.full_url, code, "error",
                                         {"X-RateLimit-Reset": "1700000000"}, None)
        return opener

    request = urllib.request.Request("https://api.github.com/repos/o/r/releases/latest")
    monkeypatch.setattr(update_protocol.urllib.request, "urlopen", failing(403))
    with pytest.raises(RuntimeError, match="HTTP 403.*1700000000"):
        update_protocol._read_small_response(request, timeout=1, maximum=10)
    assert calls == [403]

    calls.clear()
    monkeypatch.setattr(update_protocol.urllib.request, "urlopen", failing(503))
    with pytest.raises(RuntimeError, match="3 спроб"):
        update_protocol._read_small_response(request, timeout=1, maximum=10)
    assert calls == [503, 503, 503]


# ── bootstrap.sh: shell functions run in isolation ──────────────────────

BOOTSTRAP = Path("bootstrap.sh")


def shell_function(name: str) -> str:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\)\{{\n.*?^\}}\n", source, re.S | re.M)
    assert match, f"{name} is missing from bootstrap.sh"
    return match.group(0)


def run_bash(script: str, *arguments: str, **environment: str):
    env = {key: value for key, value in os.environ.items() if key != "VERTEP_ALLOW_DOWNGRADE"}
    env.update(environment)
    return subprocess.run(["bash", "-c", script, "bootstrap-test", *arguments],
                          capture_output=True, text=True, env=env, check=False)


@POSIX_BASH
@pytest.mark.parametrize("installed, candidate, allow, refused", [
    ("0.0.2.96", "0.0.2.95", "", True),
    ("0.0.2.100", "0.0.2.99", "", True),
    ("0.0.2.96", "0.0.2.96", "", False),
    ("0.0.2.96", "0.0.2.100", "", False),
    ("0.0.2.96", "0.0.3.0", "", False),
    ("0.0.2.96", "0.0.2.95", "1", False),
    (None, "0.0.2.95", "", False),
])
def test_bootstrap_refuses_to_downgrade_an_installed_system(
        tmp_path, installed, candidate, allow, refused):
    if installed:
        (tmp_path / ".env").write_text(f"OTHER=1\nVERTEP_VERSION={installed}\n", encoding="utf-8")
    script = ('fail(){ echo "$*" >&2; exit 1; }\n'
              f'INSTALL_ROOT="{tmp_path}"\n{shell_function("refuse_downgrade")}\n'
              'refuse_downgrade "$1"')
    result = run_bash(script, candidate, VERTEP_ALLOW_DOWNGRADE=allow)
    assert (result.returncode == 1) == refused, result.stderr
    if refused:
        assert "refusing to downgrade" in result.stderr


@POSIX_BASH
def test_bootstrap_leaves_flat_installations_alone(tmp_path):
    script = ('progress(){ :; }\nfail(){ echo "$*" >&2; exit 1; }\n'
              f'INSTALL_ROOT="{tmp_path}"\nsnapshot_taken=false\nbootstrap_mutated=false\n'
              f'{shell_function("snapshot_installed_system")}\n'
              f'{shell_function("activate_installed_release")}\n'
              'snapshot_installed_system 2.0.0\nactivate_installed_release /payload 2.0.0\n'
              'echo "$snapshot_taken $bootstrap_mutated"')
    (tmp_path / ".env").write_text("VERTEP_VERSION=1.0.0\n", encoding="utf-8")
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false false"


@POSIX_BASH
def test_bootstrap_activates_a_new_release_only_when_the_layout_exists(tmp_path):
    (tmp_path / "releases" / "1.0.0").mkdir(parents=True)
    (tmp_path / "current").symlink_to(Path("releases") / "1.0.0")
    log = tmp_path / "python.log"
    script = ('python3(){ echo "$*" >> "$LOG"; }\n'
              f'INSTALL_ROOT="{tmp_path}"\nbootstrap_mutated=false\n'
              f'{shell_function("activate_installed_release")}\n'
              'activate_installed_release /payload "$1"\necho "$bootstrap_mutated"')

    same = run_bash(script, "1.0.0", LOG=str(log))
    assert same.returncode == 0 and same.stdout.strip() == "false"
    assert not log.exists()

    newer = run_bash(script, "2.0.0", LOG=str(log))
    assert newer.returncode == 0 and newer.stdout.strip() == "true"
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands[0].endswith(f"release-layout.py prepare {tmp_path} /payload::2.0.0")
    assert commands[1].endswith(f"release-layout.py activate {tmp_path} 2.0.0")


@POSIX_BASH
def test_bootstrap_rolls_back_only_after_the_new_release_was_activated(tmp_path):
    (tmp_path / "scripts").mkdir()
    log = tmp_path / "vertep.log"
    cli = tmp_path / "scripts" / "vertep"
    cli.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\n', encoding="utf-8")
    cli.chmod(0o755)
    function = shell_function("rollback_after_failure")

    def call(snapshot: str, mutated: str, times: int = 1):
        script = (f'systemctl(){{ :; }}\nINSTALL_ROOT="{tmp_path}"\n'
                  f'snapshot_taken={snapshot}\nbootstrap_mutated={mutated}\nrolling_back=false\n'
                  f'{function}\n' + "rollback_after_failure\n" * times)
        return run_bash(script)

    for snapshot, mutated in (("false", "false"), ("true", "false"), ("false", "true")):
        assert call(snapshot, mutated).returncode == 0
        assert not log.exists()

    assert call("true", "true", times=2).returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["rollback"]
