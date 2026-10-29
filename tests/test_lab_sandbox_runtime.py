"""Actual sandbox image build/run; opt in with LAB_SANDBOX_DOCKER_URL.

Only a disposable Linux Docker host is supported. No host bind mounts are used.
An explicitly configured but unavailable engine is a failure, never a skip.
"""
import io
import json
import os
from pathlib import Path
import tarfile
import urllib.request
import uuid
import http.client
import socket

import pytest


def test_built_sandbox_rejects_host_access(tmp_path):
    endpoint = os.environ.get("LAB_SANDBOX_DOCKER_URL")
    if not endpoint:
        pytest.skip("requires explicit disposable Docker endpoint")

    def request(path, payload=None, method="GET", timeout=60, raw=False):
        data = payload if raw else None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(endpoint.rstrip("/") + "/v1.44" + path,
                                     data=data, method=method,
                                     headers={"Content-Type": "application/x-tar" if raw else "application/json"})
        if endpoint.startswith("unix://"):
            connection = http.client.HTTPConnection("localhost", timeout=timeout)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(timeout)
            connection.sock.connect(endpoint[len("unix://"):])
            connection.request(method, "/v1.44" + path, body=data, headers=dict(req.headers))
            response = connection.getresponse()
            body = response.read()
            assert response.status < 400, body
            connection.close()
        else:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read()
        return body if raw else json.loads(body) if body else None

    context = io.BytesIO()
    root = Path(__file__).resolve().parents[1]
    with tarfile.open(fileobj=context, mode="w") as archive:
        archive.add(root / "deploy/lab/Dockerfile.sandbox", arcname="Dockerfile")
        for path in sorted((root / "lab").rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            archive.add(path, arcname=path.relative_to(root).as_posix())
    tag = "vertep-lab-sandbox-test:" + uuid.uuid4().hex
    output = request("/build?t=" + tag, context.getvalue(), method="POST", timeout=900, raw=True)
    (tmp_path / "build.log").write_bytes(output)
    events = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert not [event for event in events if event.get("error")], output.decode(errors="replace")
    assert any("Successfully built" in event.get("stream", "") for event in events), output
    script = r'''
import json, os, pathlib, socket
from lab.workspace import Workspace, IsolationViolation
checks = {}
root = pathlib.Path('/tmp/vertep-lab/repo'); root.mkdir(parents=True)
workspace = Workspace(root)
for name in ('/srv/vertep-lab/lab/forbidden', '/etc/forbidden'):
    try: pathlib.Path(name).write_text('denied')
    except OSError: checks[name] = True
    else: checks[name] = False
checks['socket'] = not pathlib.Path('/var/run/docker.sock').exists()
checks['secrets'] = not pathlib.Path('/run/secrets').exists() and not any(key in os.environ for key in ('LAB_OWNER_TOKEN','LAB_GITHUB_TOKEN','RUNTIME_SIGNING_PRIVATE_KEY'))
try: workspace.run(['docker', 'ps'])
except IsolationViolation: checks['command'] = True
else: checks['command'] = False
try: socket.create_connection(('1.1.1.1',443),timeout=2)
except OSError: checks['network'] = True
else: checks['network'] = False
checks['allowed_command'] = workspace.run(['python','-c','print(42)']).stdout.strip() == '42'
checks['nonroot'] = os.getuid() == 65532
print(json.dumps(checks), flush=True)
assert all(checks.values()), checks
'''
    container = request("/containers/create", {"Image": tag, "Cmd": ["python", "-c", script],
        "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none", "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 64,
                       "Memory": 268435456, "Tmpfs": {"/tmp": "rw,nosuid,noexec,size=32m"}}}, method="POST")["Id"]
    try:
        request(f"/containers/{container}/start", method="POST")
        status = request(f"/containers/{container}/wait", method="POST", timeout=120)
        logs = request(f"/containers/{container}/logs?stdout=1&stderr=1", raw=True)
        (tmp_path / "sandbox.log").write_bytes(logs)
        assert status["StatusCode"] == 0, logs.decode(errors="replace")
        assert b'"network": true' in logs and b'"command": true' in logs, logs
    finally:
        request(f"/containers/{container}?force=1", method="DELETE")
        request("/images/" + tag, method="DELETE")
