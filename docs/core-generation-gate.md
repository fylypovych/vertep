# CORE Generation Gate — архітектурний контракт

> Статус: прийнято.
> Аудиторія: розробник | Канон: [AGENTS.md](../AGENTS.md) §3.1, §3.5, §14.
> Issue: `i.0.0.0.29` (#31) — «Перевірити й усунути прямі generation/provider calls у CORE».
> Дата: вересень 2026.

---

## 1. Принцип

**Vertep не генерує контент — він керує інструментами, які генерують контент.**

CORE — це мозок, диспетчер і оркестратор. Він **створює/оркеструє задачі (tasks)** та
**приймає результати (results)**. Фактична генерація контенту виконується виключно
capability-вузлами (Text / Voice / GPU / Publisher Workers) через механізм
tasks → dispatch.

Цей документ фіксує **architecture gate**: статичний контракт-тест
`tests/test_core_generation_gate.py`, який не дозволяє появу нових прямих
generation calls у `core/`.

## 2. Класифікація викликів

### 2.1 Generation / execution (заборонені в CORE)

Виклики, що виконують фактичну генерацію контенту, повинні бути диспетчеризовані:

| Слот | Метод | Capability Worker |
|---|---|---|
| LLM | `providers.llm().generate_script()` / `get_llm_client().complete()` | Text Worker |
| Script | `ScriptAgent().generate_script()` | Text Worker |
| TTS | `providers.tts().synthesize()` | Voice Worker |
| TTS runtime | `worker.role_executor.synthesize_voice()` | Voice Worker |
| GPU | `providers.compute().generate_output()` | GPU Worker |
| Image | `providers.image().generate()` | GPU Worker |
| Video (AI) | `providers.video().generate()` | GPU Worker |
| Publisher | `providers.publisher().publish()` | Publisher Worker |

Прямі звернення до адаптерів (`ComfyUIAdapter()`, `TTSAdapter()`) у CORE також заборонені.

Preview і synthesis голосу (`/api/models/voices/preview`, `/api/models/voices/synthesize`)
ставлять у чергу задачу `voice_preview` без Job (`core/voice_dispatch.py`, i.0.0.4.70 #470).
Voice Worker забирає її через `/api/tasks/claim`, виконує `execute_role_task()` і повертає
аудіо з `audio_contract/v1` через `/api/tasks/result`; CORE перевіряє sha256 і параметри
контракту. Без готового Voice Worker — `503 no_ready_voice_worker_is_available`, без відповіді
вчасно (`VOICE_PREVIEW_TIMEOUT_SECONDS`, типово 180) — `504`, помилка Worker — `502`;
fallback у CORE немає. Каталог голосів (`/voices`) лишається metadata-запитом control-plane.

### 2.2 Control-plane / orchestration (дозволені в CORE)

Дозволені операції, що не є генерацією контенту:

- **FFmpeg assembly** — `providers.video_engine().render()` у `core/pipeline.py:finalize_job()`
  (AGENTS.md §14: «Не витрачати GPU на те, що нормально робиться звичайним монтажем»).
- **Config / metadata запити** — `providers.publisher().available_channels()`,
  `providers.publisher().configured()`, `provider_matrix()`.
- **Status reporting** — `provider_matrix()` для `/api/status` та Web UI.

### 2.3 LOCAL_WORKER_FALLBACK (задокументовані винятки)

Локальні fallback-шляхи активні лише при `LOCAL_WORKER_FALLBACK=true`
**і** `VERTEP_DEMO=true` одночасно (`core/local_fallback.py`,
i.0.0.1.4 #104). Production тримає обидва `false`; production-приймання
з увімкненим fallback недійсне.

## 3. Механізм gate

Статичний тест `tests/test_core_generation_gate.py` парсить справжній
Python AST (i.0.0.1.4 #104), резолвить аліаси (`engine.render()` →
`video_engine.render`) та перевіряє кожен виклик проти `ALLOWLIST`.

Native FFmpeg assembly у CORE залишається control-plane операцією (AGENTS.md §14).
Захист від іншого VideoEngine через той самий CORE шлях (i.0.0.1.4 #104):
зовнішні рушії диспетчеризуються на Worker, а рушій з чужим `provider`
відмовляється fail-closed guard'ом `_is_native_engine()` у
`core/pipeline.py:finalize_job()` до будь-якого render.

Що ще розпізнає gate (i.0.0.4.71 #471):

- alias клієнта з `with`/`async with ... as c` (`c.post(...)` → `httpx.Client.post`);
- `importlib.import_module("…")`, `import_module("…")`, `__import__("…")` з константним ім'ям модуля;
- `getattr(module, "name")` з константним ім'ям; `getattr` з неконстантним ім'ям на модулі
  генерації (`worker.role_executor`, `adapters.comfyui`, `adapters.tts`, `adapters.llm_clients`,
  `core.gpu.backends`) — окреме порушення `DYNAMIC`;
- прямі HTTP-запити `httpx`/`requests` (`get/post/put/patch/request/stream/send`), коли
  константна частина URL називає inference endpoint: `api/generate`, `api/chat`, `api/embed(dings)`,
  `chat/completions`, `v1/completions`, `audio/speech`, `images/generations`, `synthesize`, `prompt`;
- виклики Worker-виконавців `worker.role_executor` (`synthesize_voice`, `execute_role_task`,
  `execute_text/script/storyboard/voice/image/video/publisher`).

Config/status/probe-запити (`/api/tags`, `/api/pull`, `/models`, `/voices`, `/system_stats`,
`/health`), `voice_catalog()`, `list_text_models()`, постановка задач у чергу та FFmpeg-монтаж
не є порушеннями; негативні та контрольні fixtures — у `TestBlindSpots`.

### 3.1 Межі статичного аналізу

Gate — евристика за AST, а не універсальний доказ відсутності генерації в CORE. Він не бачить:

- URL, зібраний повністю динамічно (`f"{base}/api/{operation}"`, змінна з URL, конфіг);
  так, forwarder лабораторії `core/api/lab_lease.py:inference` (gated forwarder за
  `docs/lab/lab-gpu-contract.md`) статично не розпізнається;
- HTTP-клієнти, крім `httpx`/`requests`, сокети, subprocess, сторонні SDK;
- `getattr` з неконстантним ім'ям на модулі поза переліком модулів генерації,
  `exec`/`eval`, передачу функцій як аргументів і виклики через структури даних;
- аліаси, перевизначені умовно або в іншому модулі.

Тому gate доповнює, а не замінює, архітектурний review та інтеграційні тести execution boundary
(наприклад, `tests/test_voice_worker_dispatch.py`).

### 3.2 Винятки

`ALLOWLIST` прив'язує кожен виняток до файлу, **enclosing function** (qualname, напр.
`OllamaProvider._infer`) і call-site. Новий заборонений виклик у тому самому файлі, але іншій
функції, або інша ціль у дозволеній функції виняток не успадковує; другий такий самий виклик
у дозволеній функції робить запис неоднозначним — тест падає.

| Файл | Функція | Call-site | Підстава |
|---|---|---|---|
| `core/api/job_helpers/script.py` | `_generate_script_local` | `ScriptAgent.generate_script` | LOCAL_WORKER_FALLBACK |
| `core/script_agent.py` | `ScriptAgent._complete` | `get_llm_client` | LLM-реалізація, виконується Text Worker |
| `core/pipeline.py` | `_do_publish_single` | `publisher.publish` | LOCAL_WORKER_FALLBACK |
| `core/pipeline.py` | `finalize_job` | `video_engine.render` | control-plane: native FFmpeg assembly |
| `core/gpu/backends.py` | `OllamaProvider._infer`, `OpenAIProvider._infer` | HTTP inference | shared implementation, виконується Worker GPU router |
| `core/api/lab_lease.py` | `_unload_model` | `POST /api/generate` з `keep_alive=0` | control-plane: вивантаження моделі, без inference |

Shared implementation у `core/` дозволена лише тоді, коли її не імпортує runtime CORE:
`TestSharedImplementationBoundary` перевіряє, що `core.gpu.backends` імпортує тільки
`worker/gpu_routing.py`, а не `core/`, `services/`, `adapters/`, `scripts/` чи `installer/`.

Кожен новий generation call у CORE, не внесений у `ALLOWLIST`, — помилка тестів.

## 4. Залежності та майбутня елімінація

Окремі per-stage Issues уже закривають етапи через dispatch:

- #4 — script → Text Worker
- #5 — storyboard → Text Worker
- #9 — TTS → Voice Worker
- #11 — Publisher
- #12 — GPU pipeline

Після їх завершення local fallback-шляхи (§2.3) будуть видалені, а `ALLOWLIST`
скорочено.

## 5. Результат аудиту (вересень 2026)

Стан `core/` після впровадження gate (місця вказано функціями, а не номерами рядків; звірено 16.11.2026):

| Файл | Викликання | Класифікація | Дія |
|---|---|---|---|
| `core/pipeline.py` · `finalize_job` | `providers.video_engine().render()` | control-plane (assembly) | дозволено, AGENTS.md §14 |
| `core/pipeline.py` · `_do_publish_single` | `providers.publisher().publish()` (через `_publish_local`) | LOCAL_WORKER_FALLBACK | задокументовано, буде видалено |
| `core/api/job_helpers/publish.py` · `_enqueue_publish_task` | `_publish_local()` без Publisher Worker (зокрема для публікації з Telegram) | LOCAL_WORKER_FALLBACK, лише dev/demo (`require_no_local_execution`) | задокументовано, буде видалено |
| `core/api/jobs.py` · `publish_job` | `providers.publisher().available_channels()` | control-plane (metadata) | дозволено |
| `core/api/tasks.py` · `publisher_readiness` | `providers.publisher().readiness()` | control-plane (status, без секретів) | дозволено |
| `core/api/job_helpers/script.py` · `_generate_script_local` | `ScriptAgent().generate_script()` (local) | LOCAL_WORKER_FALLBACK | allowlist |
| `core/script_agent.py` · `ScriptAgent._complete` | `get_llm_client().complete()` | LLM inference impl (Text Worker) | allowlist |
| `core/api/status.py`, `core/api/system.py` | `provider_matrix()` | control-plane (status) | дозволено |

**Висновок:** усі production generation stages у CORE оркеструють tasks і приймають
results. Прямі direct inference/synthesis/render/upload у CORE відсутні або явно
доведені як control-plane / задокументований fallback.