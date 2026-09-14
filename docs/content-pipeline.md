# Контентний конвеєр

> Статус: чинний | Аудиторія: розробник | Канон: `AGENTS.md` §18–§20

## Сценарний агент (багатоступінчастий)

1. **Structure** — заголовок, опис, хештеги, план сцен.
2. **Scenes** — окремий LLM-виклик на сцену (промпт персонажа + сценарію).
3. **Assembly** — збір фінального script, нормалізація через `ScriptDocument`.

Реалізація: `core/script_agent.py`, виклик з `core/pipeline.py:prepare_job()`. Невалідна сцена перегенеровується точково, не весь сценарій. Персонаж — конфіг, не хардкод.

## Storyboard та approval

Текстовий storyboard → image previews (GPU Worker) → revision окремих/усіх сцен → image approval. Показується сама сцена, промпт і історія версій; approval прив'язується до переглянутої версії; stale-approval відхиляється.

## GPU: ComfyUI Executor

`worker/role_executor.py` (`execute_image()`/`execute_video()`, реєстрація в `EXECUTORS`/`ROLE_TASKS`, виклик через `execute_role_task()`). Шлях: CORE → GPU Worker → ComfyUI prompt/history/view → scene-artifact → CORE, з timeout, перевіркою цілісності й медіатипу. Fallback відео — збірка з кадрів через FFmpeg. Семпл `workflows/video/demo.json` вимагає custom node Video Helper Suite (`VHS_VideoCombine`).

## Голос

Voice Worker повідомляє catalog/readiness, синтезує з параметрами персонажа (voice/provider/model/language/speed) і повертає перевірений audio-artifact; він входить у фінальне відео. Непідтримувані комбінації відхиляються явно.

## FFmpeg-монтаж

Виконується в CORE через `AssemblyProvider` (`core/pipeline.py:finalize_job()`): відеоряд, pan/zoom, переходи, голос, музика, субтитри, заставки, фінальне кодування. Принцип: GPU не витрачається на те, що робить звичайний монтаж. Довгі операції — поза event loop (ThreadPoolExecutor).

## Video review і версії

Фінальне відео проходить `VIDEO_PENDING_APPROVAL` → revision (структурована вимога конкретної версії) → regeneration як нова immutable-версія зі своїм checksum → approval поточної версії → публікація. Попередні версії доступні для перегляду; publish незатвердженого заборонено на всіх шляхах.

> Окремі дефекти lifecycle зараз у роботі — див. відкриті `i`/`ir` Issues (video revision/versioning, script/publisher contracts).
