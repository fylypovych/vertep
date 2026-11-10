"""A missing generation role must not lock the operator out of First Run."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import health_checks as hc
from core.app import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup_client(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_ROOT', str(tmp_path))
    monkeypatch.setenv('UPDATE_STATE_DIR', str(tmp_path / 'update'))
    monkeypatch.setenv('NODE_ROLE', 'core')
    monkeypatch.setenv('NODE_ADDITIONAL_ROLES', 'gpu')
    monkeypatch.setenv('SETUP_TOKEN_HASH', hashlib.sha256(b'setup-control-test').hexdigest())
    monkeypatch.delenv('SETUP_TOKEN_EXPIRES_AT', raising=False)
    monkeypatch.delenv('CORE_ADDRESS', raising=False)
    monkeypatch.setattr(hc, 'check_docker', lambda: (True, 'ok'))
    monkeypatch.setattr(hc, 'check_postgres', lambda: (True, 'ok'))
    monkeypatch.setattr(hc, 'check_redis', lambda: (True, 'ok'))
    monkeypatch.setattr(hc, 'check_gpu', lambda: (False, 'health_gpu_unavailable'))
    monkeypatch.setattr(hc, 'check_cuda', lambda: (False, 'health_gpu_runtime_failed'))
    monkeypatch.setattr(hc, 'check_comfyui', lambda: (False, 'health_endpoint_unavailable'))
    monkeypatch.setattr(hc, 'check_gpu_worker', lambda: (False, 'health_worker_not_ready'))
    return TestClient(app), tmp_path


def test_missing_gpu_preserves_setup_access_but_not_role_readiness(setup_client):
    api, _ = setup_client
    control = api.get('/api/health/control')
    assert control.status_code == 200
    assert control.json()['scope'] == 'control'
    assert control.json()['checks']['required'] == ['core_api', 'docker', 'postgres', 'redis']
    assert api.get('/api/health/ready').status_code == 503
    # The page itself is served only with a web-v2 build (local runs and Browser E2E; not local-contracts).
    if any((ROOT / p / 'index.html').is_file() for p in ('web-v2/dist/vertep-admin-v2/browser', 'web-v2/dist/vertep-admin-v2')):
        page = api.get('/setup?token=setup-control-test')
        assert page.status_code == 200
        assert 'html' in page.headers['content-type']
    assert api.get('/api/setup').status_code == 401
    headers = {'X-Vertep-Setup-Token': 'setup-control-test'}
    status = api.get('/api/setup', headers=headers)
    assert status.status_code == 200
    assert status.json()['selected_additional_roles'] == ['gpu']
    assert not api.get('/api/setup/health', headers=headers).json()['ready']
    assert api.post('/api/jobs', json={}).status_code == 503


@pytest.mark.parametrize('dependency', ['docker', 'postgres', 'redis'])
def test_control_gate_still_rejects_failed_core_dependency(setup_client, monkeypatch, dependency):
    api, _ = setup_client
    monkeypatch.setattr(hc, 'check_' + dependency, lambda: (False, 'unavailable'))
    assert api.get('/api/health/control').status_code == 503


def test_control_gate_does_not_follow_remote_worker_readiness(setup_client, monkeypatch):
    api, _ = setup_client
    monkeypatch.setenv('CORE_ADDRESS', 'https://other-core.invalid')
    monkeypatch.setattr(hc, 'check_core_api', lambda _: pytest.fail('remote role readiness used for local management'))
    assert api.get('/api/health/control').status_code == 200


def test_operator_can_remove_gpu_before_completing_setup(setup_client, monkeypatch):
    api, root = setup_client
    monkeypatch.setenv('VERTEP_VERSION', '0.0.3.60')
    headers = {'X-Vertep-Setup-Token': 'setup-control-test'}
    response = api.post('/api/setup/selection', headers=headers,
                        json={'node_role': 'core', 'ai_backend': 'skip', 'additional_roles': []})
    assert response.status_code == 200
    request = json.loads((root / 'deployment-request.json').read_text())
    assert request.get('additional_roles', []) == []
    assert api.get('/api/setup', headers=headers).json()['selected_additional_roles'] == []
    report = api.get('/api/setup/health', headers=headers).json()
    assert report['ready'] and 'gpu' not in report['required']
    assert api.get('/api/health/ready').status_code == 200
    assert not (root / 'installation.json').exists()


@pytest.mark.parametrize('roles', [None, 'gpu', ['core'], ['unknown'], [1]])
def test_invalid_manual_roles_do_not_replace_selection(setup_client, roles):
    api, root = setup_client
    response = api.post('/api/setup/selection', headers={'X-Vertep-Setup-Token': 'setup-control-test'},
                        json={'node_role': 'core', 'ai_backend': 'skip', 'additional_roles': roles})
    assert response.status_code == 422
    assert not (root / 'setup-selection.json').exists()
    assert not (root / 'deployment-request.json').exists()


def test_control_access_after_setup_does_not_admit_unready_gpu_or_unauthenticated_admin(setup_client):
    api, root = setup_client
    (root / 'installation.json').write_text(json.dumps({'completed_at': 'now', 'node_role': 'core'}))
    assert api.get('/api/health/control').status_code == 200
    assert api.get('/api/health/ready').status_code == 503
    assert api.get('/api/jobs', headers={'Authorization': ''}).status_code == 401
