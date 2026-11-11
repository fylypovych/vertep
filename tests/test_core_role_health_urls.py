"""The wizard's Health Check runs in CORE, so CORE must reach every local role service."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_core_probes_role_services_by_their_compose_names():
    services = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))["services"]
    core, worker = services["core"]["environment"], services["worker"]["environment"]
    for name in ("TTS_HEALTH_URL", "PUBLISHER_HEALTH_URL"):
        assert core.get(name) == worker.get(name), name
        assert "localhost" not in core[name]
    assert {"tts", "publisher-worker"} <= set(services)
