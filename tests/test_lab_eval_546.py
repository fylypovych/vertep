"""Tests for #546 (i.0.0.5.46): LAB Eval integration (Promptfoo + lm-evaluation-harness)."""

from __future__ import annotations

import ctypes.util
import http.server
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import urllib.request

import pytest

from lab.budget import BudgetLedger, PaidModelDenied
from lab.cli import build_parser
from lab import eval_network
from lab.eval_network import Relay, TCPRelay, isolated_endpoint, run_isolated
from lab.eval_runner import (
    EVAL_FORMAT,
    EvalIsolationError,
    LabEvalRunner,
    STRIPPED_API_KEYS,
    default_configs_dir,
    isolated_env,
    validate_eval_target,
)
from lab.policy import PolicyGate
from lab.reports import ReportBuilder


def test_isolated_env_disables_telemetry_and_strips_paid_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-paid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    env = isolated_env({"LAB_EVAL_MODEL": "llama3.2", "OPENAI_API_KEY": "should-not-leak"})

    assert env["PROMPTFOO_DISABLE_TELEMETRY"] == "1"
    assert env["PROMPTFOO_DISABLE_UPDATE"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["LAB_EVAL_MODEL"] == "llama3.2"
    for secret_key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        assert secret_key in STRIPPED_API_KEYS
        assert secret_key not in env


def test_eval_target_blocks_paid_providers_and_external_endpoints(tmp_path):
    budget = BudgetLedger(tmp_path / "budget")

    # Local Ollama on loopback / lab host succeeds
    prov, mod, ep = validate_eval_target("ollama", "llama3.2", "http://127.0.0.1:11434", budget)
    assert (prov, mod, ep) == ("ollama", "llama3.2", "http://127.0.0.1:11434")
    assert validate_eval_target("ollama", "llama3.2", "http://ollama.lab.internal:11434", budget)[2] == "http://ollama.lab.internal:11434"

    # Paid providers blocked
    for paid in ("openai", "openai-paid", "anthropic", "gemini", "paid"):
        with pytest.raises(PaidModelDenied):
            validate_eval_target(paid, "gpt-4o", "http://127.0.0.1:11434", budget)

    # Public external URL blocked by network isolation
    with pytest.raises(EvalIsolationError):
        validate_eval_target("ollama", "llama3.2", "https://api.example.com", budget)


def test_promptfoo_and_lm_eval_runners_persist_structured_reports(tmp_path):
    state_dir = tmp_path / "state"
    gate = PolicyGate(state_dir)
    budget = BudgetLedger(state_dir / "budget")
    captured_envs: list[dict] = []

    def fake_runner(cmd, **kwargs):
        captured_envs.append(kwargs.get("env") or {})
        if "--output" in cmd:
            out_path = Path(cmd[cmd.index("--output") + 1])
            out_path.write_text(
                json.dumps({
                    "results": {
                        "results": [
                            {"success": True, "score": 1.0, "latencyMs": 320, "testCase": {"description": "uk canon"}},
                            {"success": False, "score": 0.25, "latencyMs": 410, "testCase": {"description": "rag fact"}, "error": "missing year"},
                        ]
                    }
                }),
                encoding="utf-8",
            )
        elif "--output_path" in cmd:
            out_dir = Path(cmd[cmd.index("--output_path") + 1])
            (out_dir / "results_2026.json").write_text(
                json.dumps({
                    "results": {
                        "global_mmlu_uk": {"acc,none": 0.68, "acc_norm,none": 0.71},
                        "flores_uk": {"bleu,none": 29.4},
                    }
                }),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = LabEvalRunner(state_dir, gate, budget, runner=fake_runner)

    pf_cfg = default_configs_dir() / "promptfoo-uk-rag.yaml"
    assert pf_cfg.is_file()
    pf_report = runner.run_promptfoo(
        config_path=pf_cfg,
        provider="ollama",
        model="llama3.2",
        endpoint="http://127.0.0.1:11434",
        commit="a" * 40,
        actor="qa",
    )
    assert pf_report["format"] == EVAL_FORMAT
    assert pf_report["engine"] == "promptfoo"
    assert pf_report["telemetry_disabled"] is True
    assert pf_report["network_isolated"] is True
    assert pf_report["summary"]["total"] == 2
    assert pf_report["summary"]["passed"] == 1
    assert pf_report["summary"]["failed"] == 1
    assert pf_report["summary"]["pass_rate"] == 0.5
    assert len(pf_report["sha256"]) == 64

    lm_cfg = json.loads((default_configs_dir() / "lm-eval-uk.json").read_text(encoding="utf-8"))
    lm_report = runner.run_lm_eval(
        tasks=lm_cfg["tasks"],
        provider="ollama",
        model="llama3.2",
        endpoint="http://127.0.0.1:11434",
        limit=10,
        commit="b" * 40,
        actor="qa",
    )
    assert lm_report["format"] == EVAL_FORMAT
    assert lm_report["engine"] == "lm-evaluation-harness"
    assert lm_report["summary"]["task_count"] == 2
    assert lm_report["metrics"]["global_mmlu_uk"]["acc_norm,none"] == 0.71

    # Telemetry disabled in every subprocess env
    assert len(captured_envs) == 2
    for env in captured_envs:
        assert env["PROMPTFOO_DISABLE_TELEMETRY"] == "1"
        assert env["DO_NOT_TRACK"] == "1"

    # History persisted and exposed via ReportBuilder section "evals"
    assert len(runner.list_runs()) == 2
    assert "evals" in ReportBuilder.available()

    # CLI parser accepts eval subcommands
    parser = build_parser()
    parsed = parser.parse_args(["eval", "promptfoo", "--model", "llama3.2"])
    assert parsed.eval_action == "promptfoo"


def test_lab_compose_and_dockerfile_support_unprivileged_bwrap_eval_isolation():
    import yaml

    root = Path(__file__).resolve().parents[1]
    eval_net_src = (root / "lab" / "eval_network.py").read_text(encoding="utf-8")
    assert "--unshare-user-try" in eval_net_src
    assert "--unshare-net" in eval_net_src
    assert "--unshare-pid" in eval_net_src

    dockerfile = (root / "deploy" / "lab" / "Dockerfile").read_text(encoding="utf-8")
    assert "bubblewrap" in dockerfile and "libseccomp2" in dockerfile
    assert "USER 10001:10001" in dockerfile
    # lab-control stays lean: promptfoo and lm-eval live only in the opt-in eval image.
    assert "promptfoo" not in dockerfile and "lm-eval" not in dockerfile

    eval_dockerfile = (root / "deploy" / "lab" / "Dockerfile.eval").read_text(encoding="utf-8")
    assert "bubblewrap" in eval_dockerfile and "libseccomp2" in eval_dockerfile
    assert "USER 10001:10001" in eval_dockerfile
    assert "promptfoo@" in eval_dockerfile and "lm-eval==" in eval_dockerfile
    assert "PROMPTFOO_DISABLE_TELEMETRY=1" in eval_dockerfile and "DO_NOT_TRACK=1" in eval_dockerfile
    assert "download.pytorch.org/whl/cpu" in eval_dockerfile

    components = json.loads((root / "config" / "components.json").read_text(encoding="utf-8"))
    lab_extra = {item["service"]: item for item in components["components"]["lab"]["extra_images"]}
    assert lab_extra["lab-eval"]["name"] == "vertep-lab-eval"
    assert lab_extra["lab-eval"]["file"] == "deploy/lab/Dockerfile.eval"

    unconfined = {"seccomp=unconfined", "apparmor=unconfined"}
    for rel, services in (
        ("deploy/lab/docker-compose.yml", ("lab-control",)),
        ("deploy/lab/docker-compose.web.yml", ("lab-control", "lab-executor")),
    ):
        compose = yaml.safe_load((root / rel).read_text(encoding="utf-8"))
        # Services holding secrets keep Docker's default seccomp/AppArmor profiles.
        for svc_name in services:
            svc = compose["services"][svc_name]
            assert not svc.get("privileged")
            assert "ALL" in (svc.get("cap_drop") or [])
            sec_opts = set(svc.get("security_opt") or [])
            assert "no-new-privileges:true" in sec_opts
            assert not sec_opts & unconfined, f"{rel}:{svc_name} must keep default profiles"
        # Only the on-demand eval container drops them, and it holds no secrets or LAB state.
        evaluator = compose["services"]["lab-eval"]
        assert "vertep-lab-eval" in str(evaluator["image"])
        assert evaluator["profiles"] == ["eval"] and evaluator["restart"] == "no"
        assert unconfined <= set(evaluator["security_opt"]) and "no-new-privileges:true" in evaluator["security_opt"]
        assert "ALL" in evaluator["cap_drop"] and evaluator["read_only"] is True
        assert not evaluator.get("ports") and not evaluator.get("privileged")
        env = evaluator["environment"]
        names = set(env) if isinstance(env, dict) else {item.split("=", 1)[0] for item in env}
        assert not {name for name in names if "TOKEN" in name or "SECRET" in name or "GITHUB" in name}
        mounts = evaluator["volumes"]
        assert mounts == ["lab-eval-state:/var/lib/vertep-lab-eval"]


def _start_local_http_server(marker: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = marker + b":" + self.path.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def test_isolated_endpoint_tcp_over_unix_relay_and_socket_cleanup(monkeypatch):
    # Fail closed when bwrap binary is missing.
    monkeypatch.setattr(eval_network.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="bubblewrap"):
        with isolated_endpoint("http://127.0.0.1:11434"):
            pass

    # Validate endpoint rejection (empty host, https, path, public IP).
    monkeypatch.setattr(eval_network.shutil, "which", lambda _name: "/usr/bin/bwrap")
    for bad in (
        "http://",
        "https://127.0.0.1:11434",
        "http://127.0.0.1:11434/v1",
        "http://1.1.1.1:80",
    ):
        with pytest.raises(ValueError):
            with isolated_endpoint(bad):
                pass

    http_srv, http_thread, http_port = _start_local_http_server(b"LAB_MOCK_OLLAMA")
    relay_path = None
    try:
        with isolated_endpoint(f"http://127.0.0.1:{http_port}") as (local_ep, prefix, sock_path):
            relay_path = Path(sock_path)
            assert local_ep == "http://127.0.0.1:11434"
            assert "--unshare-net" in prefix
            assert relay_path.is_socket()

            # Direct Unix-socket relay check against the local HTTP server.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_client:
                unix_client.settimeout(5)
                unix_client.connect(str(relay_path))
                unix_client.sendall(b"GET /api/tags HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                chunks = []
                while data := unix_client.recv(4096):
                    chunks.append(data)
            raw_resp = b"".join(chunks)
            assert b"200 OK" in raw_resp and b"LAB_MOCK_OLLAMA:/api/tags" in raw_resp

            # Full TCP -> Unix -> TCP relay path using TCPRelay + Relay.
            def connect_unix():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(relay_path))
                return s

            with TCPRelay(("127.0.0.1", 0), Relay) as tcp_relay:
                tcp_relay.connect = connect_unix
                tcp_port = tcp_relay.server_address[1]
                tcp_thread = threading.Thread(target=tcp_relay.serve_forever, daemon=True)
                tcp_thread.start()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{tcp_port}/api/version", timeout=5) as resp:
                        assert resp.status == 200
                        assert resp.read() == b"LAB_MOCK_OLLAMA:/api/version"
                finally:
                    tcp_relay.shutdown()
                    tcp_thread.join(timeout=2)

        # Socket and temporary directory are removed on exit.
        assert relay_path is not None
        assert not relay_path.exists()
        assert not relay_path.parent.exists()
    finally:
        http_srv.shutdown()
        http_srv.server_close()
        http_thread.join(timeout=2)


def test_block_unix_sockets_enforces_seccomp_in_real_subprocess():
    if ctypes.util.find_library("seccomp") is None:
        pytest.skip("libseccomp.so.2 is not installed on this host")

    script = (
        "import errno, json, socket\n"
        "from lab.eval_network import block_unix_sockets\n"
        "block_unix_sockets()\n"
        "res = {}\n"
        "try:\n"
        "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    s.close()\n"
        "    res['af_unix'] = 'ALLOWED'\n"
        "except OSError as exc:\n"
        "    res['af_unix'] = errno.errorcode.get(exc.errno, str(exc.errno))\n"
        "with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as inet:\n"
        "    res['af_inet'] = inet.family == socket.AF_INET\n"
        "print(json.dumps(res))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    data = json.loads(proc.stdout.strip())
    assert data["af_unix"] == "EPERM"
    assert data["af_inet"] is True


def _build_bwrap_isolation_probe_script(other_port: int, writable: str, relay_dir: str) -> str:
    return f"""
import errno, json, pathlib, socket, urllib.request

result = {{}}
with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as resp:
    result["relay_status"] = resp.status
    result["relay_body"] = resp.read().decode("utf-8")

try:
    with socket.create_connection(("127.0.0.1", {other_port}), timeout=2):
        result["other_local"] = "CONNECTED"
except OSError as exc:
    result["other_local"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"

try:
    with socket.create_connection(("1.1.1.1", 80), timeout=2):
        result["external"] = "CONNECTED"
except OSError as exc:
    result["external"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"

try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.close()
    result["af_unix"] = "ALLOWED"
except OSError as exc:
    result["af_unix"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"

out_file = pathlib.Path({writable!r}) / "probe-out.json"
out_file.write_text('{{"ok": true}}', encoding="utf-8")
result["writable_ok"] = out_file.is_file()

try:
    (pathlib.Path({relay_dir!r}) / "escape.txt").write_text("no", encoding="utf-8")
    result["ro_escape"] = "WRITTEN"
except OSError as exc:
    result["ro_escape"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"

print(json.dumps(result))
"""


def _find_local_lab_eval_docker_image() -> str | None:
    if shutil.which("docker") is None:
        return None
    if subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode != 0:
        return None
    for candidate in ("vertep-lab-control:evaltest", "ghcr.io/fylypovych/vertep-lab-control:dev"):
        inspect = subprocess.run(["docker", "image", "inspect", candidate], capture_output=True, timeout=10)
        if inspect.returncode == 0:
            return candidate
    return None


def test_run_isolated_real_bwrap_namespace_and_seccomp():
    if shutil.which("bwrap") is not None:
        allowed_srv, allowed_thread, allowed_port = _start_local_http_server(b"ALLOWED_ENDPOINT")
        other_srv, other_thread, other_port = _start_local_http_server(b"OTHER_LOCAL_SERVICE")
        try:
            with tempfile.TemporaryDirectory(prefix="lab-eval-write-") as writable:
                with isolated_endpoint(f"http://127.0.0.1:{allowed_port}") as (_ep, prefix, sock_path):
                    probe = _build_bwrap_isolation_probe_script(other_port, writable, str(Path(sock_path).parent))
                    proc = run_isolated(
                        prefix,
                        sock_path,
                        writable,
                        [sys.executable, "-c", probe],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if proc.returncode != 0 and (
                        "Operation not permitted" in proc.stderr or "setting up uid map" in proc.stderr
                    ):
                        pytest.skip(f"Host kernel restricts unprivileged bwrap userns: {proc.stderr.strip()}")
                    assert proc.returncode == 0, proc.stderr
                    data = json.loads(proc.stdout.strip())
                    assert data["relay_status"] == 200
                    assert data["relay_body"] == "ALLOWED_ENDPOINT:/api/tags"
                    assert data["other_local"].startswith("BLOCKED:")
                    assert data["external"].startswith("BLOCKED:")
                    assert data["af_unix"] == "BLOCKED:EPERM"
                    assert data["writable_ok"] is True
                    assert data["ro_escape"].startswith("BLOCKED:")
                assert not Path(sock_path).exists()
        finally:
            allowed_srv.shutdown()
            allowed_srv.server_close()
            allowed_thread.join(timeout=2)
            other_srv.shutdown()
            other_srv.server_close()
            other_thread.join(timeout=2)
        return

    # bwrap is not installed on host: verify fail-closed error, then run real lab-eval container probe if image exists.
    with pytest.raises(RuntimeError, match="bubblewrap"):
        with isolated_endpoint("http://127.0.0.1:11434"):
            pass

    image = _find_local_lab_eval_docker_image()
    if image is None:
        pytest.skip("bwrap is not installed on host and no local vertep-lab-control Docker image is available")

    root = Path(__file__).resolve().parents[1]
    container_driver = """
import http.server, json, pathlib, socketserver, sys, tempfile, threading
from lab.eval_network import isolated_endpoint, run_isolated

class Handler(http.server.BaseHTTPRequestHandler):
    marker = b"ALLOWED_ENDPOINT"
    def do_GET(self):
        body = self.marker + b":" + self.path.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        return None

allowed = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
allowed_port = allowed.server_address[1]
threading.Thread(target=allowed.serve_forever, daemon=True).start()
other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
other_port = other.server_address[1]
threading.Thread(target=other.serve_forever, daemon=True).start()

try:
    with tempfile.TemporaryDirectory(prefix="lab-eval-write-") as writable:
        with isolated_endpoint(f"http://127.0.0.1:{allowed_port}") as (_ep, prefix, sock_path):
            probe = f'''
import errno, json, pathlib, socket, urllib.request
res = {{}}
with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
    res["relay_status"] = r.status
    res["relay_body"] = r.read().decode("utf-8")
try:
    with socket.create_connection(("127.0.0.1", {other_port}), timeout=2):
        res["other_local"] = "CONNECTED"
except OSError as exc:
    res["other_local"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"
try:
    with socket.create_connection(("1.1.1.1", 80), timeout=2):
        res["external"] = "CONNECTED"
except OSError as exc:
    res["external"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.close()
    res["af_unix"] = "ALLOWED"
except OSError as exc:
    res["af_unix"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"
out_file = pathlib.Path({writable!r}) / "probe-out.json"
out_file.write_text('{{"ok": true}}', encoding="utf-8")
res["writable_ok"] = out_file.is_file()
try:
    (pathlib.Path({str(pathlib.Path(sock_path).parent)!r}) / "escape.txt").write_text("no", encoding="utf-8")
    res["ro_escape"] = "WRITTEN"
except OSError as exc:
    res["ro_escape"] = f"BLOCKED:{{errno.errorcode.get(exc.errno, exc.errno)}}"
print(json.dumps(res))
'''
            proc = run_isolated(prefix, sock_path, writable, [sys.executable, "-c", probe],
                                capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0, proc.stderr
            data = json.loads(proc.stdout.strip())
        data["sock_cleaned"] = not pathlib.Path(sock_path).exists()
        print(json.dumps(data))
finally:
    allowed.shutdown()
    allowed.server_close()
    other.shutdown()
    other.server_close()
"""
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--security-opt", "seccomp=unconfined",
            "--security-opt", "apparmor=unconfined",
            "-v", f"{root / 'lab' / 'eval_network.py'}:/srv/vertep-lab/lab/eval_network.py:ro",
            image,
            "python", "-c", container_driver,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip())
    assert data["relay_status"] == 200
    assert data["relay_body"] == "ALLOWED_ENDPOINT:/api/tags"
    assert data["other_local"].startswith("BLOCKED:")
    assert data["external"].startswith("BLOCKED:")
    assert data["af_unix"] == "BLOCKED:EPERM"
    assert data["writable_ok"] is True
    assert data["ro_escape"].startswith("BLOCKED:")
    assert data["sock_cleaned"] is True
