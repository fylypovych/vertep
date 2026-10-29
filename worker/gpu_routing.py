"""Process-owned configured GPU routing; reservations survive configuration edits."""

import os
from pathlib import Path
from threading import RLock

_lock = RLock()
_router = None
_config_identity = None


def router_for(role, execute):
    global _router, _config_identity
    config = os.getenv("GPU_PROVIDERS_CONFIG")
    if not config:
        return None
    path = Path(config)
    identity = (str(path.resolve()), path.stat().st_mtime_ns)
    with _lock:
        if identity != _config_identity:
            if _router is not None and _router._active:
                return _router
            from core.gpu.backends import configured_router, local_endpoint
            def local_execute(task):
                return execute(role, task, _routing=True)
            endpoint = os.getenv("OLLAMA_URL", "http://ollama:11434") if role == "text" else os.getenv("COMFYUI_URL", "http://comfyui:8188")
            local_execute.is_local = local_endpoint(endpoint)
            _router = configured_router(path, local_execute)
            _config_identity = identity
        return _router


def cancel(task_id):
    with _lock:
        router = _router
    return router.cancel(task_id) if router is not None else False
