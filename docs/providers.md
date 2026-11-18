# Provider / Adapter Layer

> Статус: чинний | Аудиторія: розробник | Канон: [AGENTS.md](../AGENTS.md) §3.5.

Усі зовнішні движки викликаються виключно через інтерфейси в `adapters/providers/base.py`. Задні технології взаємозамінні без зміни Orchestrator, Dispatcher, Node Registry чи Web UI.

## Інтерфейси

- `LLMProvider` — генерація сценарію (`generate_script`).
- `ImageProvider` / `VideoProvider` — AI-генерація зображень/відео.
- `TTSProvider` — синтез (`synthesize`), `provider`, `configured`.
- `AssemblyProvider` — FFmpeg-монтаж (`assemble`, `assemble_clips`, `concat_audio`, `probe`).
- `ComputeProvider` — GPU-обчислення (`generate_output`, `cancel`).
- `PublisherProvider` — публікація (`publish`, `configured`, `available_channels`).
- `VideoEngine` — драйвер фінальної збірки (`render`); Job-циклом завжди володіє Vertep.

Фінальний `VideoEngine.render()` виконується у Worker через `assembly` task; CORE лише готує затверджені inputs/snapshot і приймає перевірений immutable result (#498). `video_assembly` — CPU capability, GPU не потрібен.

## Фабрики

`adapters/providers/__init__.py`: ледачий registry (`providers.llm()`, `providers.tts(name)`, `providers.assembly()`, `providers.compute()`, `providers.publisher()`, `providers.video_engine()`), `get_providers()`, `provider_matrix()` (для Web UI і `/api/status`, без мережевих викликів), `replace(name, provider)` для тестів.

## Матриця backend

| Слот | Дефолт | Альтернативи | Змінна |
|---|---|---|---|
| LLM | `ollama` | `openai` (OpenAI-сумісний) | `VERTEP_LLM_PROVIDER`, `OPENAI_API_KEY` |
| TTS | `none` | `mock`, `piper` (MIT), `kokoro` (Apache-2.0) | `TTS_PROVIDER` |
| Compute (GPU) | `vertep-worker` | `comfyui-distributed` | `VERTEP_COMPUTE_PROVIDER`, `COMFYUI_DISTRIBUTED_URL`, `COMFYUI_DISTRIBUTED_TOKEN` |
| Image / Video | `vertep-worker` | спільний з Compute | `VERTEP_COMPUTE_PROVIDER` |
| Assembly | `ffmpeg` | `ffmpeg` | — |
| VideoEngine | `native` | `money-printer` | `VERTEP_VIDEO_ENGINE`, `MONEY_PRINTER_URL`, `MONEY_PRINTER_TOKEN` |
| Publisher | `vertep-official` | Telegram + офіційні API YouTube/TikTok/FB/IG/Threads | `PUBLISHER_MOCK` / credentials платформ |

## LLM у Text Worker

Task `script` виконується на Worker через `providers.llm().generate_script()` (`DefaultLLMProvider` → багатоступінчастий `ScriptAgent` → `adapters/llm_clients.py:get_llm_client()`). Backend визначається `VERTEP_LLM_PROVIDER` на момент виконання, тож перемикання, доставлене heartbeat-ом, діє для наступного task. Протокол Ollama (`/api/generate`, `OLLAMA_SCRIPT_MODEL` → `OLLAMA_MODEL`) чи OpenAI-сумісного backend (`/chat/completions`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, ключ `OPENAI_API_KEY`/`LLM_API_KEY`) залишається в адаптері. Непідтримуваний backend або OpenAI-сумісний backend без ключа завершує task явною помилкою `LLMBackendError` без переходу на Ollama чи demo; ключ не потрапляє в artifact чи повідомлення помилки. Готовність LLM визначає одне правило `adapters/llm_clients.py:backend_readiness()` (i.0.0.4.79): `provider_matrix` показує невідомий backend під його власною назвою з `configured=false` (а не як готовий `ollama`), `openai` готовий лише за наявності ключа; Worker вважає доставлений `VERTEP_LLM_PROVIDER` застосованим лише за тим самим правилом на своєму вузлі, інакше відновлює попередню конфігурацію й повідомляє помилку активації. Значення ключів у статус не потрапляють.

## Ізольований runtime MoneyPrinterTurbo

`money-printer` — єдиний зовнішній VideoEngine, який має власний pinned-контейнер:
opt-in compose-профіль `moneyprinter`, окремий образ, заборона silent fallback на
`native`. Повний опис — [`moneyprinter-runtime.md`](moneyprinter-runtime.md).

## Ефективна конфігурація VideoEngine

Знімок ефективного движка (`selected` / `effective` / `config_revision` /
endpoint identity / посилання на secret / `ready`) доступний у
`GET /api/settings/video-engine` і в UI **Налаштування → Движки обробки
(backends)**. Значення секрету не експонується; перемикання на зовнішній рушій
приймається лише після перевірки реального runtime, інакше попередній рушій
лишається ефективним. Контракт — `core/engine_config.py`, деталі —
[`moneyprinter-runtime.md`](moneyprinter-runtime.md#ефективна-конфігурація-движка-p7).

## Правила

- Адаптери напряму в `worker/service.py` не викликаються — лише `providers.*` та `execute_role_task()`.
- Зовнішні движки — опційні, тільки за явним `.env`; інакше — дефолт.
- `LOCAL_WORKER_FALLBACK` — лише dev/demo; production-приймання з ним недійсне.
- Активна матриця: Web UI **Налаштування → Движки обробки (backends)**.
