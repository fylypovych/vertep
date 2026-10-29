"""Domain Specific Language for workflow definitions.

Provides a Python-friendly way to describe workflow DAGs
while also supporting YAML/JSON deserialization.
"""

import math


def validate_workflow(definition: dict) -> list[str]:
    """Validate a workflow definition dict.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []

    if not isinstance(definition, dict):
        errors.append("Workflow definition must be a dict")
        return errors

    name = definition.get("name")
    if not name or not isinstance(name, str):
        errors.append("Workflow must have a 'name' string field")

    tasks = definition.get("tasks")
    if not tasks or not isinstance(tasks, list):
        errors.append("Workflow must have a 'tasks' list")
        return errors

    task_ids = set()
    graph = {}
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"Task {i} must be a dict")
            continue

        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            errors.append(f"Task {i} must have an 'id' field")
        elif task_id in task_ids:
            errors.append(f"Duplicate task id: {task_id}")
        else:
            task_ids.add(task_id)

        deps = task.get("dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            errors.append(f"Task {i}: dependencies must be a list of task ids")
        elif isinstance(task_id, str):
            graph[task_id] = deps
        condition = task.get("when")
        if condition is not None and (not isinstance(condition, dict) or set(condition) != {"param", "equals"} or not isinstance(condition.get("param"), str)):
            errors.append(f"Task {i}: when must contain param and equals")
        if not isinstance(task.get("input", {}), dict):
            errors.append(f"Task {i}: input must be an object")
        vram = task.get("vram_mb", 0)
        if isinstance(vram, bool) or not isinstance(vram, int) or vram < 0:
            errors.append(f"Task {i}: vram_mb must be a nonnegative integer")
        retry = task.get("retry", {})
        if not isinstance(retry, dict):
            errors.append(f"Task {i}: retry must be an object")
        else:
            for key, default, minimum in (("max_attempts", 1, 1), ("delay_seconds", 0, 0)):
                value = retry.get(key, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
                    errors.append(f"Task {i}: invalid retry {key}")
            if not isinstance(retry.get("max_attempts", 1), int):
                errors.append(f"Task {i}: max_attempts must be an integer")
        timeout = task.get("timeout_minutes", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            errors.append(f"Task {i}: timeout_minutes must be positive")

    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            errors.append(f"Cyclic dependency at task {node}")
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep not in task_ids:
                errors.append(f"Task {node}: dependency '{dep}' not defined")
            else:
                visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)

    return errors


def parse_workflow(source: str) -> dict:
    """Read JSON/YAML definitions without executing constructors or expressions."""
    import yaml

    definition = yaml.safe_load(source)
    errors = validate_workflow(definition)
    if errors:
        raise ValueError(f"Invalid workflow definition: {errors}")
    return definition
