# Provider / Adapter Layer

> Статус: чинний | Аудиторія: розробник | Канон: `AGENTS.md` §28

Усі зовнішні движки викликаються виключно через інтерфейси в `adapters/providers/base.py`. Задні технології взаємозамінні без зміни Orchestrator, Dispatcher, Node Registry чи Web UI.

## Інтерфейси

- `LLMProvider` — генерація сценарію (`generate_script`).
- `ImageProvider` / `VideoProvider` — AI-генерація зображень/відео.
- `TTSProvider` — синтез (`synthesize`), `provider`, `configured`.
- `AssemblyProvider` — FFmpeg-монтаж (`assemble`, `assemble_clips`, `concat_audio`, `probe`).
- `ComputeProvider` — GPU-обчислення (`generate_output`, `cancel`).
- `PublisherProvider` — публікація (`publish`, `configured`, `available_channels`).
- `VideoEngine` — драйвер фінальної збірки (`render`); Job-циклом завжди володіє Vertep.

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
| VideoEngine | `native` | `money-printer`, `shortgpt` | `VERTEP_VIDEO_ENGINE`, `MONEY_PRINTER_URL`, `MONEY_PRINTER_TOKEN`, `SHORTGPT_URL`, `SHORTGPT_TOKEN` |
| Publisher | `vertep-official` | Telegram + офіційні API YouTube/TikTok/FB/IG/Threads | `PUBLISHER_MOCK` / credentials платформ |

## Правила

- Адаптери напряму в `worker/service.py` не викликаються — лише `providers.*` та `execute_role_task()`.
- Зовнішні движки — опційні, тільки за явним `.env`; інакше — дефолт.
- `LOCAL_WORKER_FALLBACK` — лише dev/demo; production-приймання з ним недійсне.
- Активна матриця: Web UI **Налаштування → Движки обробки (backends)**.
