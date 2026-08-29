import os
import json
import httpx

class LLMAdapter:
    def generate_script(self, topic: str, system_prompt: str = "") -> dict:
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            return {"title": topic, "description": f"Коротке відео: {topic}", "hashtags": ["#vertep"],
                    "voiceover": topic, "scenes": [{"prompt": topic, "video_prompt": topic, "duration": 1}]}
        prompt = (system_prompt + "\nCreate a short Ukrainian video script. Return only valid JSON with keys "
                  "title, description, hashtags, voiceover and scenes. Each scene must contain image prompt, "
                  "video_prompt and duration. Topic:\n" + topic)
        response = httpx.post(f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/generate",
                              json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"), "prompt": prompt, "format": "json", "stream": False}, timeout=300)
        response.raise_for_status()
        raw = response.json().get("response", "")
        try:
            result = json.loads(raw)
            return result if isinstance(result, dict) else {"title": topic, "raw": raw}
        except json.JSONDecodeError:
            return {"title": topic, "raw": raw}
