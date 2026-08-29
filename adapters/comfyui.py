import os
import time
import json
from pathlib import Path
import httpx

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
        path = Path(workflow_path).resolve()
        workflow_root = Path(os.getenv("WORKFLOWS_ROOT", "workflows")).resolve()
        if workflow_root not in path.parents:
            raise ValueError("Workflow path escapes WORKFLOWS_ROOT")
        if not path.exists():
            raise FileNotFoundError(f"ComfyUI workflow not found: {workflow_path}")
        workflow = json.loads(path.read_text(encoding="utf-8"))
        # API workflows may use {{TOPIC}} in any string input.
        serialized = json.dumps(workflow)
        serialized = serialized.replace("{{TOPIC}}", topic.replace('"', '\\"'))
        serialized = serialized.replace("{{CHECKPOINT}}", os.getenv("COMFYUI_CHECKPOINT", "model.safetensors"))
        serialized = serialized.replace("{{SEED}}", os.getenv("COMFYUI_SEED", "42"))
        serialized = serialized.replace("{{WIDTH}}", os.getenv("COMFYUI_WIDTH", "768"))
        serialized = serialized.replace("{{HEIGHT}}", os.getenv("COMFYUI_HEIGHT", "432"))
        workflow = json.loads(serialized)
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
        try:
            response = httpx.post(f"{os.getenv('COMFYUI_URL', 'http://localhost:8188')}/interrupt", timeout=10)
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            return False
