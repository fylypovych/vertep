import json
import re
from pathlib import Path

WORKFLOW_TYPES = {"image", "video", "character"}
SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\.json$")


def validate_workflow(workflow: dict) -> list[str]:
    errors = []
    if not isinstance(workflow, dict) or not workflow:
        return ["Workflow must be a non-empty object"]
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            errors.append(f"Node {node_id} must be an object")
            continue
        if not isinstance(node.get("class_type"), str):
            errors.append(f"Node {node_id} has no class_type")
        if not isinstance(node.get("inputs", {}), dict):
            errors.append(f"Node {node_id} inputs must be an object")
    serialized = json.dumps(workflow)
    for placeholder in re.findall(r"\{\{([A-Z0-9_]+)\}\}", serialized):
        if placeholder not in {"TOPIC", "CHECKPOINT", "SEED", "WIDTH", "HEIGHT"}:
            errors.append(f"Unsupported placeholder: {placeholder}")
    return errors


class WorkflowRegistry:
    def __init__(self, root: str | Path = "workflows"):
        self.root = Path(root)

    def list(self) -> list[dict]:
        result = []
        for kind in WORKFLOW_TYPES:
            for path in (self.root / kind).glob("*.json"):
                try:
                    workflow = json.loads(path.read_text(encoding="utf-8"))
                    errors = validate_workflow(workflow)
                except (OSError, ValueError) as error:
                    errors = [str(error)]
                result.append({"type": kind, "name": path.name, "path": f"workflows/{kind}/{path.name}",
                               "valid": not errors, "errors": errors})
        return sorted(result, key=lambda item: (item["type"], item["name"]))

    def load(self, kind: str, name: str) -> dict:
        self._validate_path(kind, name)
        return json.loads((self.root / kind / name).read_text(encoding="utf-8"))

    def save(self, kind: str, name: str, workflow: dict) -> dict:
        self._validate_path(kind, name)
        errors = validate_workflow(workflow)
        if errors:
            raise ValueError("; ".join(errors))
        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"type": kind, "name": name, "valid": True}

    @staticmethod
    def _validate_path(kind: str, name: str) -> None:
        if kind not in WORKFLOW_TYPES or not SAFE_NAME.fullmatch(name):
            raise ValueError("Invalid workflow type or name")
