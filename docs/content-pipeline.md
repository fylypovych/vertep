# Контентний конвеєр

> Статус: чинний | Аудиторія: розробник | Канон: [AGENTS.md](../AGENTS.md) §3.1, §3.5, §14.

## Сценарний агент (багатоступінчастий)

1. **Structure** — заголовок, опис, хештеги, план сцен.
2. **Scenes** — окремий LLM-виклик на сцену (промпт персонажа + сценарію).
3. **Assembly** — збір фінального script, нормалізація через `ScriptDocument`.

Нормативний контракт — Structure / Scenes / Assembly, точкова перегенерація невалідної сцени та конфіг персонажа ([AGENTS.md](../AGENTS.md) §14). Фактичний production-шлях: `core/pipeline.py:generate_script()` → `core/api/job_helpers/script.py:_enqueue_script_task()` → Worker task `script` → `worker/role_executor.py:execute_script()`. `execute_script()` викликає `providers.llm()` (обраний `VERTEP_LLM_PROVIDER` backend, див. [providers](providers.md)), а той — `core/script_agent.py:ScriptAgent`, який виконує Structure (окремий запит), кожну Scene окремим запитом і Assembly через `normalize_script()`/`ScriptDocument`; конфіг персонажа та `revision` передаються в кожен етап. Метадані структури (`title`, `description`, `hashtags`, `voiceover`, `platforms`) перевіряються тим самим контрактом `normalize_script()`/`ScriptDocument`, що й Assembly, одразу після Structure — до генерації сцен (i.0.0.4.80); допустима нормалізація та сама, що в Assembly (порожній `title` → тема, `hashtags` рядком → розбиття за пробілами). `duration: null` у сцені означає «значення немає», як відсутній ключ, і замінюється запланованою тривалістю без перегенерації; невалідне число (≤0, >600) як і раніше перегенеровує сцену. `task.prompt` для task `script` — необов'язковий додатковий brief («Завдання: …» у кожному етапі), а не заміна поетапного prompt; CORE і workflow runtime його зараз не задають. Невалідна сцена перегенеровується окремо до `SCRIPT_SCENE_MAX_ATTEMPTS` (типово 3) спроб, коректні сцени зберігаються; невалідна структура, вичерпані спроби сцени чи помилка backend завершують task помилкою, і CORE застосовує свій `max_retries`. `timeout` задачі (типово `OLLAMA_SCRIPT_TIMEOUT`) — загальний бюджет усього прогону Structure → Scenes, а не кожного запиту (i.0.0.4.74): перед кожним запитом (структура, наступна сцена, повтор сцени) Worker перевіряє скасування від CORE, втрату lease і залишок бюджету, і після втрати права новий LLM-запит не стартує; timeout поточного запиту обрізається до залишку бюджету. Запит, що вже виконується, не переривається — зупинка настає щонайпізніше після його завершення або спливу бюджету. Скасування дає термінальний `cancelled` result, вичерпаний бюджет — звичайну помилку, до якої CORE застосовує `max_retries`. CORE не виконує LLM-генерацію напряму.

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
