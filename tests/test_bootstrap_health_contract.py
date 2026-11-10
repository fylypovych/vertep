"""Current bootstrap must honor the selected signed runtime's health contract."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('vertep-host-health.service', 'vertep-host-health.timer',
            'scripts/host-health.py', 'core/health_observation.py')


def select_contract(tmp_path, compose, files):
    bash, jq = shutil.which('bash'), shutil.which('jq')
    if not bash or not jq:
        pytest.skip('requires POSIX bash and jq')
    (tmp_path / 'docker-compose.yml').write_text(compose, encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'files': {name: {'sha256': 'a' * 64} for name in files}}))
    source = (ROOT / 'bootstrap.sh').read_text(encoding='utf-8')
    function = re.search(r'^select_runtime_health_contract\(\)\{\n.*?^\}', source, re.M | re.S).group()
    script = ('set -eu\nINSTALL_ROOT=$1\nmanifest_tmp=$2\n'
              'fail(){ echo "$*" >&2; exit 23; }\n' + function +
              '\nselect_runtime_health_contract\nprintf "%s\\n" "$runtime_health_path" "${host_health_units[@]}"\n')
    return subprocess.run([bash, '-c', script, 'bootstrap-health-test', str(tmp_path), str(manifest)],
                          capture_output=True, text=True, timeout=10)


def test_published_legacy_runtime_needs_no_new_host_units(tmp_path):
    result = select_contract(tmp_path, 'healthcheck: http://localhost:8080/api/health\n', ())
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['/api/health']


def test_new_runtime_requires_observer_and_readiness_endpoint(tmp_path):
    result = select_contract(tmp_path, 'HOST_HEALTH_OBSERVATION: /data/config/host-health/observation.json\n', REQUIRED)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['/api/health/ready', *REQUIRED[:2]]


@pytest.mark.parametrize('missing', REQUIRED)
def test_partial_new_runtime_cannot_downgrade_to_legacy_liveness(tmp_path, missing):
    result = select_contract(tmp_path, 'HOST_HEALTH_OBSERVATION: /data/config/host-health/observation.json\n',
                             [name for name in REQUIRED if name != missing])
    assert result.returncode == 23
    assert 'observer contract is incomplete' in result.stderr


def test_preexisting_readiness_contract_still_uses_readiness(tmp_path):
    result = select_contract(tmp_path, 'healthcheck: http://localhost:8080/api/health/ready\n', ())
    assert result.returncode == 0 and result.stdout.split() == ['/api/health/ready']
