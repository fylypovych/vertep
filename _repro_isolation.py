"""Quick reproduction of test_role_isolation_failure_is_reported on Windows."""
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('q', 'scripts/qualify-release.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
root = Path('.')
tmp = Path(tempfile.mkdtemp())
old = '"services": ["worker", "comfyui", "update-agent"]'
new = '"services": ["worker", "comfyui", "postgres", "update-agent"]'
catalog = (root / 'config/node_roles.json').read_text().replace(old, new)
names = ('bootstrap.sh', 'deploy/docker-compose.yml', 'deploy/proxy.conf',
         'config/schemas/release-contract.schema.json', 'scripts/runtime-contract.py',
         'scripts/generate-sbom.py', 'scripts/apply-deployment.py',
         'installer/vertep-deployment.service', 'installer/vertep-deployment.path',
         'services/tts_service.py', 'services/publisher_service.py',
         'services/backup_service.py', 'docker/tts/Dockerfile', 'docker/publisher/Dockerfile',
         'docker/backup/Dockerfile', 'docker/proxy/entrypoint.sh',
         'monitoring/prometheus.yml', 'monitoring/alerts.yml', 'monitoring/loki.yml',
         'monitoring/promtail.yml',
         'monitoring/grafana/provisioning/datasources/vertep.yml',
         'monitoring/grafana/provisioning/dashboards/vertep.yml',
         'monitoring/grafana/dashboards/fleet.json', 'scripts/update-agent.py',
         'scripts/release-layout.py', 'installer/update-public.pem')
for name in names:
    t = tmp / name
    t.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(root / name, t)
(tmp / 'config').mkdir(exist_ok=True)
(tmp / 'config/node_roles.json').write_text(catalog)
r = m.qualify(tmp)
print(json.dumps([c for c in r['checks'] if 'gpu' in c['name'] or not c['passed']], indent=1))
print('--- local repo same check ---')
print(json.dumps([c for c in m.qualify(root)['checks'] if not c['passed']], indent=1))