"""Execute the bootstrap platform selector without mutating the host."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('distro,version,expected', [
    ('ubuntu', '24.04', ('29.1.3-0ubuntu3~24.04.2', '2.40.3+ds1-0ubuntu1~24.04.1')),
    ('ubuntu', '26.04', ('29.1.3-0ubuntu4.1', '2.40.3+ds1-0ubuntu1')),
    ('ubuntu', '22.04', None),
    ('ubuntu', '26.10', None),
    ('debian', '26.04', None),
    ('', '', None),
])
def test_platform_package_selection(distro, version, expected):
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('requires bash')
    source = (ROOT / 'bootstrap.sh').read_text(encoding='utf-8')
    selector = re.search(r'^select_host_platform\(\)\{\n.*?^\}', source, re.M | re.S).group()
    result = subprocess.run([
        bash, '-c', 'set -eu\nfail(){ exit 23; }\n' + selector +
        '\nID=$1\nVERSION_ID=$2\nselect_host_platform\n'
        'printf "%s\\n" "$DOCKER_APT_VERSION" "$COMPOSE_APT_VERSION"',
        'platform-test', distro, version,
    ], text=True, capture_output=True, timeout=10)
    if expected is None:
        assert result.returncode == 23, result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == list(expected)


def test_platform_and_amd_guards_precede_host_mutation():
    source = (ROOT / 'bootstrap.sh').read_text(encoding='utf-8')
    stop = source.index('for trigger in vertep-update.path')
    assert source.index('\nselect_host_platform\n') < stop
    assert source.index('Automatic AMD ROCm installation on Ubuntu 26.04') < stop
    assert stop < source.index('apt-get update -qq')
