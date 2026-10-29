"""Disposable executor: stock heartbeat/apply and executor calls, isolated process."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import httpx

from worker import service
from worker.role_executor import execute_role_task
from core.engine_config import engine_config_revision
from adapters.providers import providers


def main():
    root = Path(os.environ["PROBE_ROOT"])
    instance = uuid.uuid4().hex
    done = set()
    with httpx.Client(base_url=os.environ["CORE_URL"], headers={"X-Vertep-Token": os.environ["NODE_API_TOKEN"]}, timeout=15) as client:
        while True:
            try:
                control = client.post("/api/workers/heartbeat", json={
                    "node_name": "config-executor", "role": "core", "status": "FREE",
                    "runtime_instance_id": instance, "capabilities": ["speech_synthesis", "video_generation"],
                    "provider_config": service.executor_provider_config(),
                    "provider_config_revision": service._PROVIDER_CONFIG_REVISION,
                    "provider_engine_revision": service.executor_engine_revision(),
                    "provider_activation_error": service._PROVIDER_ACTIVATION_ERROR}).json()
                service.apply_provider_config(control.get("provider_config"), control.get("provider_config_revision", ""))
                engine = providers.video_engine()
                state = {"pid": os.getpid(), "instance": instance,
                    "observed": service.executor_provider_config(), "revision": service._PROVIDER_CONFIG_REVISION,
                    "activation_error": service._PROVIDER_ACTIVATION_ERROR,
                    "engine": engine.engine_id, "engine_revision": engine_config_revision(engine)}
                command_file = root / "command.json"
                command = json.loads(command_file.read_text(encoding="utf-8")) if command_file.exists() else {}
                if command.get("id") and command["id"] not in done:
                    done.add(command["id"])
                    try:
                        if command["type"] == "voice":
                            artifacts = execute_role_task("voice", command["task"])
                            result = {"contract": artifacts[0]["contract"]}
                        else:
                            from core.engine_config import verify_engine_snapshot
                            if command.get("snapshot"):
                                verify_engine_snapshot(engine, command["snapshot"])
                            inputs = {k: [Path(x) for x in v] if k in {"images", "clips"} and v else
                                Path(v) if k in {"audio", "music", "subtitles", "watermark"} and v else v
                                for k, v in command["inputs"].items()}
                            output = root / f"{command['id']}.mp4"
                            engine.render(output, **inputs)
                            result = {"engine": engine.engine_id, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "output": str(output)}
                    except Exception as error:
                        result = {"error": type(error).__name__}
                    (root / f"{command['id']}.json").write_text(json.dumps(result), encoding="utf-8")
                temporary = root / "observation.tmp"
                temporary.write_text(json.dumps(state), encoding="utf-8")
                temporary.replace(root / "observation.json")
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.1)


if __name__ == "__main__":
    main()
