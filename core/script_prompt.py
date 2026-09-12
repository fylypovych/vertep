import os

from .models import Job

PROMPT_VERSION = "1"


def build_script_prompt(topic: str, system_prompt: str, character: dict | None) -> str:
    target = float(os.getenv("SCRIPT_TARGET_DURATION", "60"))
    context = {
        "topic": topic,
        "character": character or {},
        "aspect_ratio": character.get("visual", {}).get("aspect_ratio") if character else "16:9",
        "target_duration_seconds": target,
    }
    system = system_prompt.strip() if system_prompt else ""
    return (
        "Ти створюєш сценарій короткого українськомовного відео. "
        "Поверни лише JSON object без Markdown: title, description, hashtags, "
        "voiceover та scenes. Кожна scene містить prompt, video_prompt, voiceover, duration. "
        "duration — додатне число; сумарна тривалість має бути близькою до target_duration_seconds. "
        "Дотримуйся характеру персонажа та глобальних правил безпеки.\n"
        + (f"{system}\n" if system else "")
        + f"CONTEXT={__import__('json').dumps(context, ensure_ascii=False)}"
    )
