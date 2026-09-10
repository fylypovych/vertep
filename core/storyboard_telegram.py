from pathlib import Path

from .models import Job, StoryboardVersion


def render_storyboard(job: Job, storyboard: StoryboardVersion, limit: int = 3900) -> list[str]:
    header = (f"🎬 Розкадровка {job.job_id}, версія {storyboard.version} · превʼю v{storyboard.image_version} ({storyboard.image_status})\n"
              f"{storyboard.title}\n{storyboard.description}".strip())
    blocks = [header]
    for scene in storyboard.scenes:
        img = f"Превʼю: {scene.image_artifact_id or 'генерується'} (v{scene.image_version or storyboard.image_version})"
        blocks.append(
            f"Сцена {scene.index} · {scene.duration:g} с\n"
            f"Текст: {scene.voiceover or '—'}\n"
            f"Кадр: {scene.prompt}\n"
            f"Рух: {scene.video_prompt}\n"
            f"{img}"
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


def send_storyboard_images(chat_id: str, job: Job, storyboard: StoryboardVersion, store_root: Path) -> None:
    from adapters.telegram import TelegramAdapter
    adapter = TelegramAdapter()
    if not adapter.configured():
        return
    for scene in storyboard.scenes:
        if not scene.image_artifact_id:
            continue
        folder = Path(job.job_id) / "storyboard" / f"v{storyboard.image_version}"
        candidates = [
            store_root / folder / f"scene-{scene.index:03d}-v{storyboard.image_version}.png",
            store_root / folder / f"scene-{scene.index:03d}-v{storyboard.image_version}.jpg",
        ]
        photo_path = next((p for p in candidates if p.exists()), None)
        if not photo_path:
            continue
        caption = f"Сцена {scene.index} · {storyboard.title}"
        try:
            adapter.send_photo(str(chat_id), str(photo_path), caption=caption[:1024])
        except Exception:
            pass


def storyboard_keyboard(job_id: str, version: int, image_version: int | None = None) -> dict:
    # Keep legacy callbacks; add image storyboard controls
    base = [
        {"text": "✅ Схвалити", "callback_data": f"sb_ok:{job_id}:{version}"},
        {"text": "🔄 Перегенерувати", "callback_data": f"sb_regen:{job_id}:{version}"},
        {"text": "✍️ Запросити правки", "callback_data": f"sb_edit:{job_id}:{version}"},
        {"text": "❌ Відхилити", "callback_data": f"sb_reject:{job_id}:{version}"},
    ]
    image_row = [
        {"text": "🖼️ Схвалити превʼю", "callback_data": f"sb_img_ok:{job_id}:{version}"},
        {"text": "🔁 Перегенерувати превʼю", "callback_data": f"sb_img_regen:{job_id}:{version}"},
        {"text": "✏️ Правки превʼю", "callback_data": f"sb_img_edit:{job_id}:{version}"},
    ]
    return {"inline_keyboard": [base, image_row]}


def image_storyboard_keyboard(job_id: str, version: int, scene_index: int | None = None) -> dict:
    if scene_index is not None:
        return {"inline_keyboard": [[
            {"text": f"🔁 Сцена {scene_index}", "callback_data": f"sb_img_scene:{job_id}:{version}:{scene_index}"},
            {"text": "🖼️ Схвалити превʼю", "callback_data": f"sb_img_ok:{job_id}:{version}"},
        ]]}
    return storyboard_keyboard(job_id, version)
