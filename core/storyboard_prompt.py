import json
import os

from .models import Job


PROMPT_VERSION = "1"


def build_storyboard_prompt(job: Job, character: dict, brand: dict | None = None,
                            revision: str | None = None) -> str:
    target_duration = int(os.getenv("STORYBOARD_TARGET_DURATION", "60"))
    context = {
        "topic": job.topic,
        "brand": brand or {"id": job.brand_id},
        "character": character,
        "aspect_ratio": job.aspect_ratio,
        "output_preset": job.output_preset,
        "target_duration_seconds": target_duration,
        "revision_request": revision,
    }
    return (
        "Ти створюєш розкадровку короткого українськомовного відео. "
        "Поверни лише JSON object без Markdown: title, description, hashtags та scenes. "
        "Кожна scene обов'язково містить prompt, video_prompt, voiceover, duration. "
        "duration має бути додатним числом; сумарна тривалість має бути близькою до target_duration_seconds. "
        "Дотримуйся характеру персонажа, стилю бренду та глобальних правил безпеки.\n"
        f"CONTEXT={json.dumps(context, ensure_ascii=False)}"
    )
