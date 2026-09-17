import os
import time
import json
from pathlib import Path
import httpx

def _substitute_placeholders(obj, mapping: dict[str, str]):
    """Recursively replace ``{{TOKEN}}`` placeholders inside a workflow structure.

    This operates on the parsed workflow (dict/list) rather than on its JSON
    serialization, so any ``topic`` value (newlines, backslashes, quotes,
    Unicode) is carried verbatim without risking a ``JSONDecodeError`` or an
    unterminated string before the prompt is submitted.
    """
    if isinstance(obj, dict):
        return {key: _substitute_placeholders(value, mapping) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_substitute_placeholders(item, mapping) for item in obj]
    if isinstance(obj, str):
        for token, replacement in mapping.items():
            obj = obj.replace(f"{{{{{token}}}}}", replacement)
        return obj
    return obj


class ComfyUIAdapter:
    def __init__(self) -> None:
        self.current_prompt_id: str | None = None

    def submit(self, workflow: dict) -> dict:
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            return {"prompt_id": "demo", "status": "STUB"}
        response = httpx.post(f"{os.getenv('COMFYUI_URL', 'http://localhost:8188')}/prompt",
                              json={"prompt": workflow}, timeout=30)
        response.raise_for_status()
        return response.json()

    def wait_for_result(self, prompt_id: str, timeout: int = 300) -> dict:
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            return {"prompt_id": prompt_id, "status": "STUB"}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = httpx.get(f"{os.getenv('COMFYUI_URL', 'http://localhost:8188')}/history/{prompt_id}", timeout=10)
            response.raise_for_status()
            history = response.json()
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(2)
        raise TimeoutError(f"ComfyUI prompt timed out: {prompt_id}")

    def generate(self, workflow_path: str, topic: str) -> tuple[bytes, str]:
        data, filename, kind = self.generate_output(workflow_path, topic, "image")
        if kind != "image":
            raise RuntimeError("ComfyUI workflow did not return an image")
        return data, filename

    def generate_output(self, workflow_path: str, topic: str, task_type: str = "image") -> tuple[bytes, str, str]:
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            if task_type != "image":
                raise RuntimeError("Demo mode has no synthetic video workflow")
            width, height = 640, 360
            return (f"P6\n{width} {height}\n255\n".encode() + bytes((34, 54, 48)) * width * height,
                    "scene-001.ppm", "image")
        workflow_root = Path(os.getenv("WORKFLOWS_ROOT", "workflows")).resolve()
        p = Path(workflow_path)
        if p.is_absolute():
            path = p.resolve()
        else:
            if p.parts and p.parts[0] == "workflows":
                cand1 = (workflow_root / Path(*p.parts[1:])).resolve()
                cand2 = (workflow_root.parent / p).resolve()
                cand3 = (workflow_root / p).resolve()
                path = cand1 if cand1.exists() else (cand2 if cand2.exists() else cand1)
            else:
                path = (workflow_root / p).resolve()
        if path != workflow_root and workflow_root not in path.parents and not (workflow_root.name == "workflows" and workflow_root.parent in path.parents):
            raise ValueError("Workflow path escapes WORKFLOWS_ROOT")
        if not path.exists():
            fallback = (Path("workflows") / (Path(*p.parts[1:]) if p.parts and p.parts[0] == "workflows" else p)).resolve()
            if fallback.exists() and (Path("workflows").resolve() in fallback.parents or fallback == Path("workflows").resolve()):
                path = fallback
            else:
                raise FileNotFoundError(f"ComfyUI workflow not found: {workflow_path}")
        workflow = json.loads(path.read_text(encoding="utf-8"))
        # API workflows may use {{TOPIC}}/{{CHECKPOINT}}/{{SEED}}/{{WIDTH}}/{{HEIGHT}}
        # in any string input. Substitute inside the workflow structure so that
        # arbitrary ``topic`` values (newlines, backslashes, quotes, Unicode)
        # cannot corrupt the serialized JSON.
        workflow = _substitute_placeholders(workflow, {
            "TOPIC": topic,
            "CHECKPOINT": os.getenv("COMFYUI_CHECKPOINT", "model.safetensors"),
            "SEED": os.getenv("COMFYUI_SEED", "42"),
            "WIDTH": os.getenv("COMFYUI_WIDTH", "768"),
            "HEIGHT": os.getenv("COMFYUI_HEIGHT", "432"),
        })
        submitted = self.submit(workflow)
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI returned no prompt_id")
        self.current_prompt_id = prompt_id
        try:
            result = self.wait_for_result(prompt_id)
        finally:
            self.current_prompt_id = None
        output_keys = ("images",) if task_type == "image" else ("videos", "gifs")
        for node in result.get("outputs", {}).values():
            for output_key in output_keys:
                for artifact in node.get(output_key, []):
                    response = httpx.get(f"{os.getenv('COMFYUI_URL', 'http://localhost:8188')}/view",
                                         params={"filename": artifact["filename"], "subfolder": artifact.get("subfolder", ""),
                                                 "type": artifact.get("type", "output")}, timeout=120)
                    response.raise_for_status()
                    return response.content, Path(artifact["filename"]).name, task_type
        raise RuntimeError(f"ComfyUI completed without a {task_type} output")

    def cancel(self) -> bool:
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            self.current_prompt_id = None
            return True
        url = os.getenv("COMFYUI_URL", "http://localhost:8188")
        # Try prompt-scoped removal first (queued prompts that have not started
        # executing can be removed individually without interrupting other work).
        if self.current_prompt_id:
            try:
                response = httpx.delete(
                    f"{url}/queue",
                    params={"prompt_id": self.current_prompt_id},
                    timeout=10)
                response.raise_for_status()
                self.current_prompt_id = None
                return True
            except httpx.HTTPError:
                pass  # prompt may already be executing; fall through to global interrupt
        # Global interrupt as fallback for the currently-executing prompt.
        try:
            response = httpx.post(f"{url}/interrupt", timeout=10)
            response.raise_for_status()
            self.current_prompt_id = None
            return True
        except httpx.HTTPError:
            return False
