import os
import json
from typing import Any

import httpx

from .script_schema import normalize_script, ScriptDocument


class ScriptAgent:
    def generate_script(self, topic: str, system_prompt: str = "", character: dict | None = None) -> dict:
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            return {"title": topic, "description": f"Коротке відео: {topic}", "hashtags": ["#vertep"],
                    "voiceover": topic, "scenes": [{"prompt": topic, "video_prompt": topic, "duration": 1}]}
        metadata = self._generate_metadata(topic, system_prompt, character)
        scenes = self._generate_scenes(topic, system_prompt, character, metadata)
        payload = {**metadata, "scenes": scenes}
        return normalize_script(payload, topic)

    def _generate_metadata(self, topic: str, system_prompt: str, character: dict | None) -> dict:
        prompt = self._build_prompt(system_prompt, character, f"""
Згенеруй структуру сценарію для короткого відео на тему: {topic}
Поверни лише JSON з полями:
- title: короткий заголовок
- description: опис для соцмереж
- hashtags: масив хештегів
- voiceover: загальний текст озвучки (якщо є)
- scenes_plan: масив з 3-5 планів сцен, кожен з полями: index, summary, estimated_duration
""")
        response = httpx.post(f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/generate",
                              json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"), "prompt": prompt,
                                    "format": "json", "stream": False}, timeout=300)
        response.raise_for_status()
        raw = response.json().get("response", "")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"title": topic, "description": "", "hashtags": [], "voiceover": ""}
        return {
            "title": str(data.get("title") or topic),
            "description": str(data.get("description") or ""),
            "hashtags": data.get("hashtags") or [],
            "voiceover": str(data.get("voiceover") or ""),
        }

    def _generate_scenes(self, topic: str, system_prompt: str, character: dict | None, metadata: dict) -> list[dict]:
        plan = metadata.get("scenes_plan") or [{"index": 1, "summary": topic, "estimated_duration": 5}]
        scenes = []
        for entry in plan:
            index = entry.get("index") if isinstance(entry, dict) else None
            summary = entry.get("summary") if isinstance(entry, dict) else str(entry)
            duration = float(entry.get("estimated_duration", 5)) if isinstance(entry, dict) else 5.0
            scene = self._generate_scene(topic, system_prompt, character, metadata, index, summary, duration)
            scenes.append(scene)
        return scenes if scenes else [{"prompt": topic, "video_prompt": topic, "voiceover": metadata.get("voiceover", ""), "duration": 5}]

    def _generate_scene(self, topic: str, system_prompt: str, character: dict | None, metadata: dict,
                        index: int | None, summary: str, duration: float) -> dict:
        prompt = self._build_prompt(system_prompt, character, f"""
Тема відео: {topic}
Загальний заголовок: {metadata.get('title', topic)}
Загальна озвучка: {metadata.get('voiceover', '')}

Згенеруй детальний опис сцени {index + 1 if index is not None else 1}:
Поверни лише JSON з полями:
- prompt: детальний image prompt для генерації зображення
- video_prompt: короткий опис для відео-анімації
- voiceover: текст озвучки для цієї сцени
- duration: тривалість в секундах (макс 600)
""")
        response = httpx.post(f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/generate",
                              json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"), "prompt": prompt,
                                    "format": "json", "stream": False}, timeout=300)
        response.raise_for_status()
        raw = response.json().get("response", "")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"prompt": summary, "video_prompt": summary, "voiceover": "", "duration": duration}
        return {
            "prompt": str(data.get("prompt") or summary),
            "video_prompt": str(data.get("video_prompt") or data.get("prompt") or summary),
            "voiceover": str(data.get("voiceover") or metadata.get("voiceover") or ""),
            "duration": max(0.5, min(600, float(data.get("duration", duration)))),
        }

    @staticmethod
    def _build_prompt(system_prompt: str, character: dict | None, user_prompt: str) -> str:
        parts = []
        if system_prompt:
            parts.append(system_prompt.strip())
        if character:
            style = character.get("visual", {}).get("style") or character.get("style")
            if style:
                parts.append(f"Візуальний стиль: {style}")
            generation = character.get("generation", {})
            if generation.get("workflow"):
                parts.append(f"Workflow: {generation['workflow']}")
        parts.append(user_prompt.strip())
        return "\n\n".join(parts)
