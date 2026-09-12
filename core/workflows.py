import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

WORKFLOW_TYPES = {"image", "video", "character"}
SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\.json$")
VERSION_DIR = "versions"

SUPPORTED_PLACEHOLDERS = {"TOPIC", "CHECKPOINT", "SEED", "WIDTH", "HEIGHT"}


def validate_workflow(workflow: dict) -> Dict[str, Any]:
    errors = []
    warnings = []
    schema_info = {
        "node_count": 0,
        "node_types": [],
        "has_placeholders": [],
        "estimated_vram_mb": 0,
    }

    if not isinstance(workflow, dict) or not workflow:
        return {"valid": False, "errors": ["Workflow must be a non-empty object"], "warnings": [], "schema": schema_info}

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            errors.append(f"Node {node_id} must be an object")
            continue
        schema_info["node_count"] += 1
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            errors.append(f"Node {node_id} has no class_type")
        else:
            schema_info["node_types"].append(class_type)
        if not isinstance(node.get("inputs", {}), dict):
            errors.append(f"Node {node_id} inputs must be an object")
        if "meta" in node and isinstance(node["meta"], dict):
            if "title" in node["meta"]:
                pass

    serialized = json.dumps(workflow)
    for placeholder in re.findall(r"\{\{([A-Z0-9_]+)\}\}", serialized):
        if placeholder not in SUPPORTED_PLACEHOLDERS:
            errors.append(f"Unsupported placeholder: {placeholder}")
        else:
            if placeholder not in schema_info["has_placeholders"]:
                schema_info["has_placeholders"].append(placeholder)

    if schema_info["node_count"] == 0:
        warnings.append("Workflow has no nodes")

    if schema_info["node_count"] > 50:
        warnings.append(f"Large workflow ({schema_info['node_count']} nodes), consider splitting")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "schema": schema_info,
    }


class WorkflowRegistry:
    def __init__(self, root: str | Path = "workflows"):
        self.root = Path(root)

    def list(self) -> List[Dict]:
        result = []
        for kind in WORKFLOW_TYPES:
            for path in (self.root / kind).glob("*.json"):
                if path.name.endswith(".meta.json") or path.parent.name == VERSION_DIR:
                    continue
                try:
                    workflow = json.loads(path.read_text(encoding="utf-8"))
                    validation = validate_workflow(workflow)
                except (OSError, ValueError) as error:
                    validation = {"valid": False, "errors": [str(error)], "warnings": [], "schema": {}}
                result.append({
                    "type": kind,
                    "name": path.name,
                    "path": f"workflows/{kind}/{path.name}",
                    **validation,
                })
        return sorted(result, key=lambda item: (item["type"], item["name"]))

    def usage(self, kind: str, name: str) -> dict:
        self._validate_path(kind, name)
        reference = f"workflows/{kind}/{name}"
        from core.state import store
        dependent_jobs = [
            {"job_id": job.job_id, "topic": job.topic, "status": job.status.value}
            for job in store.jobs.values()
            if job.workflow == reference
        ]
        character_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
        dependent_characters = []
        if character_root.exists():
            for directory in character_root.iterdir():
                if directory.is_dir():
                    for fname in ("character.json", "generation.json"):
                        fpath = directory / fname
                        if fpath.exists():
                            try:
                                data = json.loads(fpath.read_text(encoding="utf-8"))
                                if data.get("workflow") == reference:
                                    dependent_characters.append(
                                        {"character_id": directory.name, "field": fname.replace(".json", "")})
                                    break
                            except Exception:
                                pass
        return {
            "workflow": reference,
            "jobs": dependent_jobs,
            "characters": dependent_characters,
            "total_jobs": len(dependent_jobs),
            "total_characters": len(dependent_characters),
        }

    def load(self, kind: str, name: str) -> dict:
        self._validate_path(kind, name)
        path = self.root / kind / name
        workflow = json.loads(path.read_text(encoding="utf-8"))
        validation = validate_workflow(workflow)
        return {
            "workflow": workflow,
            "validation": validation,
            "meta": self._load_meta(kind, name),
        }

    def save(self, kind: str, name: str, workflow: dict, force: bool = False) -> dict:
        self._validate_path(kind, name)
        validation = validate_workflow(workflow)
        if not validation["valid"]:
            raise ValueError("; ".join(validation["errors"]))

        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name

        if path.exists() and not force:
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing_validation = validate_workflow(existing)
            if existing_validation["valid"]:
                self._archive_version(kind, name, existing)
                raise ValueError(
                    "Workflow already exists and is valid. Use force=true to overwrite. "
                    "Previous version archived."
                )

        if path.exists():
            self._archive_version(kind, name, json.loads(path.read_text(encoding="utf-8")))

        path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        self._save_meta(kind, name, {"updated_at": datetime.now(timezone.utc).isoformat()})
        return {
            "type": kind,
            "name": name,
            "valid": True,
            "validation": validation,
        }

    def delete(self, kind: str, name: str, force: bool = False) -> dict:
        self._validate_path(kind, name)
        reference = f"workflows/{kind}/{name}"

        from core.state import store
        dependent_jobs = [
            {"job_id": job.job_id, "topic": job.topic, "status": job.status.value}
            for job in store.jobs.values()
            if job.workflow == reference
        ]

        character_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
        dependent_characters = []
        if character_root.exists():
            for directory in character_root.iterdir():
                if directory.is_dir():
                    char_data = {}
                    for fname in ("character.json", "generation.json"):
                        fpath = directory / fname
                        if fpath.exists():
                            try:
                                data = json.loads(fpath.read_text(encoding="utf-8"))
                                if data.get("workflow") == reference:
                                    dependent_characters.append({"character_id": directory.name, "field": fname.replace(".json", "")})
                                    break
                            except Exception:
                                pass

        dependencies = {
            "jobs": dependent_jobs,
            "characters": dependent_characters,
        }

        has_dependencies = bool(dependent_jobs or dependent_characters)

        if has_dependencies and not force:
            references = ", ".join(
                f"персонаж {item['character_id']}" for item in dependent_characters
            ) or ", ".join(f"завдання {item['job_id']}" for item in dependent_jobs) or "інші ресурси"
            raise ValueError(
                "Робочий процес використовується у: " + references
                + " (referenced by other resources); використовуйте force=true to delete anyway."
            )

        path = self.root / kind / name
        if not path.exists():
            raise FileNotFoundError("Workflow not found")

        self._archive_version(kind, name, json.loads(path.read_text(encoding="utf-8")), deleted=True)
        path.unlink()
        meta_path = self.root / kind / VERSION_DIR / f"{name}.meta.json"
        if meta_path.exists():
            meta_path.unlink()

        return {
            "deleted": reference,
            "dependencies": dependencies if has_dependencies else None,
            "archived": True,
        }

    def get_versions(self, kind: str, name: str) -> List[Dict]:
        self._validate_path(kind, name)
        versions_dir = self.root / kind / VERSION_DIR
        if not versions_dir.exists():
            return []

        versions = []
        for vpath in sorted(versions_dir.glob(f"{name}.v*.json")):
            try:
                version_num = int(vpath.stem.split(".v")[-1])
                meta_path = versions_dir / f"{name}.v{version_num}.meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                versions.append({
                    "version": version_num,
                    "archived_at": meta.get("archived_at"),
                    "deleted": meta.get("deleted", False),
                    "size_bytes": vpath.stat().st_size,
                })
            except Exception:
                pass
        return sorted(versions, key=lambda v: v["version"], reverse=True)

    def load_version(self, kind: str, name: str, version: int) -> dict:
        self._validate_path(kind, name)
        versions_dir = self.root / kind / VERSION_DIR
        vpath = versions_dir / f"{name}.v{version}.json"
        if not vpath.exists():
            raise FileNotFoundError(f"Version {version} not found")
        workflow = json.loads(vpath.read_text(encoding="utf-8"))
        validation = validate_workflow(workflow)
        meta_path = versions_dir / f"{name}.v{version}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return {
            "workflow": workflow,
            "validation": validation,
            "meta": meta,
        }

    def restore_version(self, kind: str, name: str, version: int) -> dict:
        self._validate_path(kind, name)
        versions_dir = self.root / kind / VERSION_DIR
        vpath = versions_dir / f"{name}.v{version}.json"
        if not vpath.exists():
            raise FileNotFoundError(f"Version {version} not found")
        workflow = json.loads(vpath.read_text(encoding="utf-8"))
        validation = validate_workflow(workflow)
        if not validation["valid"]:
            raise ValueError(f"Archived version {version} is invalid: {validation['errors']}")
        return self.save(kind, name, workflow, force=True)

    def get_form_schema(self, kind: str, name: str) -> dict:
        self._validate_path(kind, name)
        path = self.root / kind / name
        if not path.exists():
            raise FileNotFoundError("Workflow not found")
        workflow = json.loads(path.read_text(encoding="utf-8"))
        validation = validate_workflow(workflow)

        form_fields = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            if isinstance(inputs, dict):
                for input_name, input_value in inputs.items():
                    if isinstance(input_value, (str, int, float, bool)):
                        field_type = type(input_value).__name__
                        form_fields.append({
                            "node_id": node_id,
                            "input_name": input_name,
                            "current_value": input_value,
                            "type": field_type,
                            "class_type": node.get("class_type"),
                        })

        return {
            "type": kind,
            "name": name,
            "validation": validation,
            "form_fields": form_fields,
            "schema": validation["schema"],
        }

    def _archive_version(self, kind: str, name: str, workflow: dict, deleted: bool = False) -> None:
        versions_dir = self.root / kind / VERSION_DIR
        versions_dir.mkdir(parents=True, exist_ok=True)

        existing_versions = list(versions_dir.glob(f"{name}.v*.json"))
        next_version = len(existing_versions) + 1

        vpath = versions_dir / f"{name}.v{next_version}.json"
        vpath.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")

        meta = {
            "version": next_version,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "deleted": deleted,
        }
        meta_path = versions_dir / f"{name}.v{next_version}.meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_meta(self, kind: str, name: str) -> dict:
        meta_path = self.root / kind / f"{name}.meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_meta(self, kind: str, name: str, meta: dict) -> None:
        meta_path = self.root / kind / f"{name}.meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _validate_path(kind: str, name: str) -> None:
        if kind not in WORKFLOW_TYPES or not SAFE_NAME.fullmatch(name):
            raise ValueError("Invalid workflow type or name")