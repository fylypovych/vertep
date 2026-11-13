"""Controlled LLM answers for the staged Text Worker script (Structure -> Scenes).

Process tests replace only the Ollama HTTP runtime.  A single prepared script
document is split into the stage answers the production ``execute_script``
requests; any other prompt (e.g. storyboard) receives the whole document.
"""

import re

from core.script_agent import SCENE_STAGE, STRUCTURE_STAGE


def staged_response(prompt: str, script: dict) -> dict:
    if STRUCTURE_STAGE in prompt:
        structure = {key: value for key, value in script.items() if key != "scenes"}
        structure["scenes_plan"] = [
            {"summary": scene.get("prompt") or f"scene {index}",
             "estimated_duration": scene.get("duration", 5)}
            for index, scene in enumerate(script.get("scenes") or [], 1)]
        return structure
    if SCENE_STAGE in prompt:
        number = int(re.search(r"SCENE_INDEX=(\d+)", prompt).group(1))
        return script["scenes"][number - 1]
    return script
