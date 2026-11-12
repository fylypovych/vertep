"""#445: automated consumer-level readiness, not physical GPU acceptance."""
import importlib.util
import json
import subprocess
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import health_checks as hc
from core.health_observation import read_observation, write_observation

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(monkeypatch, tmp_path):
    from core.api import setup, observability
    monkeypatch.setenv('CONFIG_ROOT', str(tmp_path))
    monkeypatch.setenv('UPDATE_STATE_DIR', str(tmp_path / 'update'))
    monkeypatch.setenv('NODE_ROLE', 'core')
    monkeypatch.delenv('NODE_ADDITIONAL_ROLES', raising=False)
    monkeypatch.setattr(hc, 'check_postgres', lambda: (True, 'ok'))
    monkeypatch.setattr(hc, 'check_redis', lambda: (True, 'ok'))
    monkeypatch.setattr(hc, 'check_ollama', lambda url=None: (False, 'health_endpoint_unavailable'))
    monkeypatch.setattr(hc, 'check_comfyui', lambda: (True, 'ok'))
    monkeypatch.setattr(hc, 'check_gpu_worker', lambda: (True, 'health_worker_ready'))
    monkeypatch.setattr(observability, '_run_health_checks', hc.run_checks)
    monkeypatch.setattr(observability, '_health_status', hc.health_status)
    monkeypatch.delenv('CORE_ADDRESS', raising=False)
    path = tmp_path / 'host-health' / 'observation.json'
    monkeypatch.setenv('HOST_HEALTH_OBSERVATION', str(path))
    write_observation(path, {'docker': True, 'gpu': True, 'cuda': True})
    app = FastAPI()
    app.include_router(setup.router)
    app.include_router(observability.router)
    return TestClient(app), tmp_path, path


@pytest.mark.parametrize('fault', ['missing', 'stale', 'future', 'malformed', 'negative', 'boolean_time', 'nan', 'missing_check', 'wrong_schema', 'oversized'])
def test_observation_failure_and_recovery_reaches_all_consumers(client, fault):
    api, root, path = client
    good = {'schema': 1, 'observed_at': time.time(), 'checks': {'docker': True}}
    if fault == 'missing':
        path.rename(path.with_suffix('.saved'))
    else:
        if fault == 'stale': good['observed_at'] -= 100
        if fault == 'future': good['observed_at'] += 100
        if fault == 'negative': good['checks']['docker'] = False
        if fault == 'boolean_time': good['observed_at'] = True
        if fault == 'nan': good['observed_at'] = float('nan')
        if fault == 'missing_check': good['checks'] = {}
        if fault == 'wrong_schema': good['schema'] = 2
        path.write_text('bad-json' if fault == 'malformed' else 'x' * 20000 if fault == 'oversized' else json.dumps(good), encoding='utf-8')
    setup = api.get('/api/setup/health').json()
    assert not setup['ready'] and setup['status'] == 'UNHEALTHY'
    assert setup['details']['docker']['required']
    # Information endpoint remains compatible, but the actual gate is 503.
    assert api.get('/api/health').status_code == 200
    assert api.get('/api/health').json()['status'] == 'UNHEALTHY'
    assert api.get('/api/health/ready').status_code == 503
    write_observation(path, {'docker': True})
    assert api.get('/api/setup/health').json()['ready']
    assert api.get('/api/health/ready').status_code == 200


def test_stopped_probe_expires_even_when_last_result_was_success(client, monkeypatch):
    import core.health_observation as observation
    api, _, path = client
    stamp = json.loads(path.read_text())['observed_at']
    monkeypatch.setattr(observation.time, 'time', lambda: stamp + 91)
    assert api.get('/api/health/ready').status_code == 503


def test_native_host_still_runs_real_cli_boundary(monkeypatch):
    monkeypatch.delenv('HOST_HEALTH_OBSERVATION', raising=False)
    monkeypatch.setattr(hc.Path, 'exists', lambda self: False)
    calls = []
    monkeypatch.setattr(hc.subprocess, 'run', lambda cmd, **kw: calls.append((cmd, kw)))
    assert hc.check_docker()[0]
    assert calls[0][0] == ['docker', 'info'] and calls[0][1]['check']
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired('docker', 10)
    monkeypatch.setattr(hc.subprocess, 'run', unavailable)
    assert not hc.check_docker()[0]


def test_container_without_host_source_does_not_fallback_to_local_cli(monkeypatch):
    monkeypatch.delenv('HOST_HEALTH_OBSERVATION', raising=False)
    monkeypatch.setattr(hc.Path, 'exists', lambda self: True)
    monkeypatch.setattr(hc.subprocess, 'run', lambda *a, **k: pytest.fail('local CLI used'))
    assert not hc.check_docker()[0]


@pytest.mark.parametrize('backend,extras,ollama_required', [
    ('skip', ['gpu'], False), ('external', ['gpu'], False), ('openai', [], False),
    ('ollama', [], True), ('external', ['text'], True),
])
def test_selected_roles_and_backend_drive_api_gate(client, backend, extras, ollama_required):
    from core.deployment_plan import create_plan
    from core.node_registry import node_roles
    api, root, _ = client
    plan = create_plan(node_roles(), 'core', '0.0.3.57', extras)
    (root / 'deployment-plan.json').write_text(json.dumps(plan), encoding='utf-8')
    (root / 'installation.json').write_text(json.dumps({'ai_backend': {'type': backend}}), encoding='utf-8')
    report = api.get('/api/setup/health').json()
    assert ('ollama' in report['required']) is ollama_required
    assert report['ready'] is (not ollama_required)
    if not ollama_required:
        assert report['checks']['ollama'] == 'OPTIONAL'
    assert api.get('/api/health/ready').status_code == (503 if ollama_required else 200)


def test_wizard_selection_changes_and_preserves_additional_gpu(client, monkeypatch):
    from core.deployment_plan import create_plan
    from core.node_registry import node_roles
    api, root, _ = client
    monkeypatch.setenv('VERTEP_VERSION', '0.0.3.57')
    plan = create_plan(node_roles(), 'core', '0.0.3.57', ['gpu'])
    (root / 'deployment-plan.json').write_text(json.dumps(plan), encoding='utf-8')
    for backend in ['skip', 'ollama', 'external']:
        response = api.post('/api/setup/selection', json={'node_role': 'core', 'ai_backend': backend})
        assert response.status_code == 200
        request = json.loads((root / 'deployment-request.json').read_text())
        assert 'gpu' in request['additional_roles']
        report = api.get('/api/setup/health', params={'backend': backend}).json()
        assert ('ollama' in report['required']) is (backend == 'ollama')
        assert report['ready'] is (backend != 'ollama')
        assert not (root / 'installation.json').exists()
    # The pending selection does not overwrite the executor's applied plan.
    assert json.loads((root / 'deployment-plan.json').read_text()) == plan


def test_selected_gpu_failure_is_required_and_worker_separate(client):
    api, _, path = client
    write_observation(path, {'docker': True, 'gpu': True, 'cuda': False})
    report = api.get('/api/setup/health?additional_roles=gpu&backend=skip').json()
    assert not report['ready']
    assert report['checks']['gpu'] == 'OK' and report['checks']['comfyui'] == 'OK'
    assert report['checks']['cuda'].startswith('OFFLINE')
    assert report['details']['cuda']['required']
    assert report['checks']['gpu_worker'] == 'OPTIONAL'  # Enrollment follows setup.


def test_wizard_checks_contain_only_probes_not_selection_metadata(client):
    api, _, _ = client
    report = api.get('/api/setup/health?additional_roles=gpu&backend=skip').json()
    assert report['ready'] is True
    assert 'selected_roles' not in report['checks'] and 'selected_roles' not in report['details']
    assert all(value == 'OK' or value == 'OPTIONAL' for value in report['checks'].values())


def test_applied_plan_supersedes_stale_environment_and_pending_selection(client, monkeypatch):
    from core.deployment_plan import create_plan
    from core.node_registry import node_roles
    api, root, _ = client
    monkeypatch.setenv('NODE_ADDITIONAL_ROLES', 'gpu,text')
    (root / 'installation.json').write_text(json.dumps({'completed_at': 'now', 'ai_backend': {'type': 'external'}}))
    (root / 'setup-selection.json').write_text(json.dumps({'role': 'core', 'additional_roles': ['gpu', 'text']}))
    (root / 'deployment-plan.json').write_text(json.dumps(create_plan(node_roles(), 'core', '0.0.3.57')))
    report = api.get('/api/setup/health').json()
    assert report['ready'] and 'cuda' not in report['required'] and 'ollama' not in report['required']


def test_wizard_selection_does_not_persist_backend_key(client):
    api, root, _ = client
    response = api.post('/api/setup/selection', json={'ai_backend': 'external', 'backend_api_key': 'secret-key'})
    assert response.status_code == 200
    assert 'secret-key' not in response.text
    for path in root.glob('*.json'):
        assert 'secret-key' not in path.read_text(encoding='utf-8')


def test_gpu_worker_registration_and_self_test_remain_required_after_setup(client, monkeypatch):
    api, root, _ = client
    (root / 'installation.json').write_text(json.dumps({'completed_at': 'now'}))
    monkeypatch.setenv('NODE_ADDITIONAL_ROLES', 'gpu')
    monkeypatch.setattr(hc, 'check_gpu_worker', lambda: (False, 'health_worker_not_ready'))
    report = api.get('/api/setup/health').json()
    assert not report['ready'] and report['details']['gpu_worker']['required']


def test_native_cuda_computes_without_nvcc(monkeypatch):
    monkeypatch.delenv('HOST_HEALTH_OBSERVATION', raising=False)
    monkeypatch.setattr(hc.subprocess, 'run', lambda *a, **k: pytest.fail('nvcc called'))
    class Tensor:
        def __matmul__(self, other): return self
        def __eq__(self, other): return self
        def all(self): return self
        def item(self): return True
    tensor = Tensor()
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True, synchronize=lambda: None),
                            ones=lambda *a, **k: tensor)
    monkeypatch.setitem(sys.modules, 'torch', torch)
    assert hc.check_cuda()[0]
    torch.cuda.synchronize = lambda: (_ for _ in ()).throw(RuntimeError('driver failure'))
    assert not hc.check_cuda()[0]


def test_host_probe_runs_inside_actual_comfyui_and_publishes_no_secrets(tmp_path, monkeypatch):
    module = load_script('host-health.py')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/deployment-plan.json').write_text(json.dumps({'role': 'core', 'additional_roles': ['gpu']}))
    calls = []
    def runner(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=json.dumps({'gpu': True, 'cuda': False, 'password': 'must-not-leak'}))
    monkeypatch.setattr(module.subprocess, 'run', runner)
    result = module.observe(tmp_path)
    assert result == {'docker': True, 'gpu': True, 'cuda': False}
    assert calls[0] == ['docker', 'info']
    assert calls[1][-6:-2] == ['exec', '-T', 'comfyui', 'python']
    assert 'nvcc' not in module.GPU_PROBE and 'synchronize' in module.GPU_PROBE
    assert 'must-not-leak' not in json.dumps(result)


def test_invalid_selection_does_not_write_request(client):
    api, root, _ = client
    for body in ({'node_role': 'bogus'}, {'ai_backend': 'unknown'},
                 {'backend_url': 'https://user:password@example.com'}):
        assert api.post('/api/setup/selection', json=body).status_code == 422
    assert not (root / 'deployment-request.json').exists()
    assert not (root / 'setup-selection.json').exists()


@pytest.mark.parametrize('fault', ['none', 'http_only', 'stale', 'revoked', 'missing_registration', 'unrelated_node'])
def test_gpu_worker_gate_uses_local_enrollment_and_measured_self_test(monkeypatch, fault):
    from datetime import datetime, timedelta, timezone
    from core import node_registry
    from core.state import store
    stamp = datetime.now(timezone.utc)
    checked_at = stamp - timedelta(hours=1) if fault == 'stale' else stamp
    worker = {'node_name': 'local-worker', 'role': 'core', 'status': 'READY',
              'last_seen': stamp.isoformat(), 'capabilities': ['image_generation'],
              'tested_capabilities': ['image_generation'],
              'self_test': {'role': 'core', 'status': 'PASSED', 'checked_at': checked_at.isoformat(),
                            'evidence': {'gpu': {'kind': 'image', 'bytes': 32, 'sha256': 'a' * 64}}}}
    records = [{'node_id': 'local-worker', 'status': 'READY'}]
    if fault == 'http_only': worker['self_test']['evidence'] = {}
    if fault == 'revoked': records[0]['revoked_at'] = stamp.isoformat()
    if fault == 'missing_registration': records = []
    if fault == 'unrelated_node': worker['node_name'] = 'another-worker'
    monkeypatch.setenv('NODE_NAME', 'local-worker')
    monkeypatch.setenv('NODE_MTLS_REQUIRED', 'true')
    monkeypatch.setattr(store, 'load_workers', lambda: [worker])
    monkeypatch.setattr(node_registry, 'registered_nodes', lambda: records)
    assert hc.check_gpu_worker()[0] is (fault == 'none')


def test_core_gpu_self_test_executes_gpu_workflow_not_just_http(monkeypatch):
    from worker import service
    monkeypatch.setenv('NODE_ADDITIONAL_ROLES', 'gpu')
    monkeypatch.setenv('DEMO_MODE', 'false')
    monkeypatch.setattr(service.httpx, 'get', lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None))
    monkeypatch.setattr(service, 'verify_gpu_runtime_inventory', lambda: None)
    class Compute:
        def __init__(self): self.calls = 0
        def generate_output(self, *args):
            self.calls += 1
            return b'a' * 32, 'image/png', 'image'
    compute = Compute()
    result = service.role_self_test('core', {'gpu_available': True}, compute)
    assert result['status'] == 'PASSED' and compute.calls == 1
    assert result['evidence']['gpu']['bytes'] == 32
    failed = service.role_self_test('core', {'gpu_available': False}, compute)
    assert failed['status'] == 'FAILED'


@pytest.mark.parametrize('failure', ['manager', 'daemon', 'compose', 'version'])
def test_bootstrap_capability_gate_refuses_before_mutations(tmp_path, failure):
    bash = shutil.which('bash') or str(Path('C:/Program Files/Git/bin/bash.exe'))
    if not Path(bash).is_file():
        pytest.skip('Bash unavailable')
    source = (ROOT / 'bootstrap.sh').read_text(encoding='utf-8')
    gate = source[source.index('service_manager=${'):source.index('. /etc/os-release')]
    # Run the actual preflight with controlled executable boundaries. The
    # executable exists in every case; a failed manager/daemon is not presence.
    stubs = '''
set -eu
fail(){ echo "REFUSED: $*"; exit 17; }
systemctl(){ echo "CALL systemctl $*" >&2; [[ "$FAULT" != manager ]]; }
docker(){
  echo "CALL docker $*" >&2
  if [[ "$1" == info ]]; then [[ "$FAULT" != daemon ]];
  elif [[ "$1" == compose ]]; then [[ "$FAULT" != compose ]] && echo v2.29.6;
  elif [[ "$1" == version ]]; then [[ "$FAULT" == version ]] && echo 23.0.0 || echo 27.3.1;
  else echo MUTATION >&2; return 99; fi
}
'''
    import os
    process = subprocess.run([bash, '-c', stubs + gate], env={**os.environ, 'FAULT': failure},
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 17 and 'REFUSED:' in process.stdout
    assert 'MUTATION' not in process.stderr
    assert 'systemctl stop' not in process.stderr and 'restart' not in process.stderr
    assert source.index('service_manager=${') < source.index('systemctl stop')


def test_signed_bundle_contains_complete_observer_contract(tmp_path):
    import hashlib
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from core.release_contract import validate_release_contract
    import yaml
    builder = load_script('build-runtime-bundle.py')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / 'private.pem'
    public = tmp_path / 'public.pem'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    roles = json.loads((ROOT / 'config/node_roles.json').read_text())
    services = {item for definition in roles.values() if isinstance(definition, dict) for item in definition.get('services', [])}
    lock = tmp_path / 'images.json'
    lock.write_text(json.dumps({name: {'reference': f'ghcr.io/fylypovych/vertep/{name}',
                  'digest': 'sha256:' + 'a' * 64, 'platforms': ['linux/amd64']} for name in services}))
    version = (ROOT / 'VERSION').read_text().strip()
    bundle = builder.build(ROOT, tmp_path / 'bundle', version, 1000, lock, private, 'a' * 40)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    validate_release_contract(manifest, bundle, public)
    for name in ('scripts/host-health.py', 'core/health_observation.py',
                 'vertep-host-health.service', 'vertep-host-health.timer', 'docs/runtime-readiness.md'):
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == manifest['files'][name]['sha256']
    compose = yaml.safe_load((bundle / 'docker-compose.yml').read_text())
    core = compose['services']['core']
    assert core['healthcheck']['test'][-1].endswith('/api/health/control')
    assert any('host-health:/data/config/host-health:ro' in mount for mount in core['volumes'])
    assert not any('docker.sock' in mount for mount in core['volumes'])
    assert 'host-health.py' in (bundle / 'scripts/vertep').read_text(encoding='utf-8')
    # Tampering with the actual shipped probe must fail signature/file validation.
    probe = bundle / 'scripts/host-health.py'
    altered = bytearray(probe.read_bytes())
    altered[0] ^= 1
    probe.write_bytes(altered)
    with pytest.raises(RuntimeError, match='digest mismatch'):
        validate_release_contract(manifest, bundle, public)
