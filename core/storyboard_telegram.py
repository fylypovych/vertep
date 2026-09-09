from .models import Job, StoryboardVersion


def render_storyboard(job: Job, storyboard: StoryboardVersion, limit: int = 3900) -> list[str]:
    header = (f"🎬 Розкадровка {job.job_id}, версія {storyboard.version}\n"
              f"{storyboard.title}\n{storyboard.description}".strip())
    blocks = [header]
    for scene in storyboard.scenes:
        blocks.append(
            f"Сцена {scene.index} · {scene.duration:g} с\n"
            f"Текст: {scene.voiceover or '—'}\n"
            f"Кадр: {scene.prompt}\n"
            f"Рух: {scene.video_prompt}"
        )
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


def storyboard_keyboard(job_id: str, version: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Схвалити", "callback_data": f"sb_ok:{job_id}:{version}"},
        {"text": "🔄 Перегенерувати", "callback_data": f"sb_regen:{job_id}:{version}"},
        {"text": "✍️ Запросити правки", "callback_data": f"sb_edit:{job_id}:{version}"},
        {"text": "❌ Відхилити", "callback_data": f"sb_reject:{job_id}:{version}"},
    ]]}
