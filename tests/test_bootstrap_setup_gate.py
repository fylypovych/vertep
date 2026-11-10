"""Exercise bootstrap's actual shell gate with a failed optional GPU container."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('bash'), 'requires POSIX bash')
class BootstrapSetupGateTests(unittest.TestCase):
    def run_gate(self, configured=False, modern=True, core_failed=False):
        source = (ROOT / 'bootstrap.sh').read_text(encoding='utf-8')
        select = re.search(r'^select_bootstrap_health_contract\(\)\{\n.*?^\}', source, re.M | re.S).group()
        required = re.search(r'^required_services_healthy\(\)\{\n.*?^\}', source, re.M | re.S).group()
        with tempfile.TemporaryDirectory(prefix='vertep-bootstrap-control-') as directory:
            root = Path(directory)
            (root / 'docker-compose.yml').write_text('/api/health/control' if modern else '/api/health/ready')
            # Docker is a controlled process boundary: the selected GPU service
            # is restarting. Only a first-run control gate may ignore it.
            script = '''set -eu
INSTALL_ROOT=$1
installation_complete=$2
core_failed=$3
runtime_health_path=/api/health/ready
role_services=(proxy core migrate comfyui)
BOOTSTRAP_SERVICES=(proxy core migrate)
compose=(fake_compose)
fake_compose(){ printf '%s\\n' "${@: -1}"; }
docker(){
  local name=${@: -1}
  if [[ $name == comfyui ]]; then printf 'restarting unhealthy\\n'
  elif [[ $name == core && $core_failed == true ]]; then printf 'running unhealthy\\n'
  else printf 'running healthy\\n'; fi
}
''' + select + '\n' + required + '''
select_bootstrap_health_contract
printf 'probe=%s\\n' "$bootstrap_health_path"
required_services_healthy
'''
            return subprocess.run(['bash', '-c', script, 'setup-gate', str(root),
                                   str(configured).lower(), str(core_failed).lower()],
                                  text=True, capture_output=True, timeout=10)

    def test_initial_wizard_accepts_restarting_optional_gpu(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('probe=/api/health/control', result.stdout)

    def test_initial_wizard_still_rejects_failed_core(self):
        result = self.run_gate(core_failed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('service core', result.stderr)

    def test_configured_installation_keeps_full_gpu_gate(self):
        result = self.run_gate(configured=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('probe=/api/health/ready', result.stdout)
        self.assertIn('service comfyui', result.stderr)

    def test_legacy_runtime_cannot_silently_skip_full_readiness(self):
        result = self.run_gate(modern=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('probe=/api/health/ready', result.stdout)


if __name__ == '__main__':
    unittest.main()
