"""Domain Specific Language for workflow definitions.

Provides a Python-friendly way to describe workflow DAGs
while also supporting YAML/JSON deserialization.
"""


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
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"Task {i} must be a dict")
            continue

        task_id = task.get("id")
        if not task_id:
            errors.append(f"Task {i} must have an 'id' field")
        elif task_id in task_ids:
            errors.append(f"Duplicate task id: {task_id}")
        else:
            task_ids.add(task_id)

        deps = task.get("dependencies", [])
        for dep in deps:
            if dep not in task_ids and dep not in [t.get("id") for t in tasks[:i]]:
                errors.append(f"Task {task_id}: dependency '{dep}' not defined")

    return errors