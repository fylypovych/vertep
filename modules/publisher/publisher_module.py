"""PublisherModule — керування чинною функцією publisher через модульний lifecycle (#497).

Не містить нової логіки: виконання делегується тому самому виконавцю ролі
(`worker/role_executor.py`) через `modules.execution.execute_bound`.
"""

from __future__ import annotations

from typing import Any


class PublisherModule:
    def __init__(self, provider: Any | None = None, executor=None) -> None:
        self.provider = provider
        self._bound_executor = executor
        self._active_tasks: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "publisher"

    def health_check(self) -> dict[str, Any]:
        return {"module": "publisher", "status": "READY" if self._bound_executor else "UNBOUND",
                "active_tasks": len(self._active_tasks)}

    def resource_query(self) -> dict[str, Any]:
        return {"module": "publisher", "min_vram_mb": 0, "cpu_cores": 1, "ram_mb": 512, "gpu_required": False}

    def execute(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        if self._bound_executor is None:
            raise RuntimeError("PublisherModule runs only through the bound role executor")
        from modules.execution import execute_bound
        return execute_bound(self, task)

    def cancel(self, task_id: str) -> bool:
        return self._active_tasks.pop(task_id, None) is not None
