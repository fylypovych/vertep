# CORE Generation Gate — архітектурний контракт

> Статус: прийнято.
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
| GPU | `providers.compute().generate_output()` | GPU Worker |
| Image | `providers.image().generate()` | GPU Worker |
| Video (AI) | `providers.video().generate()` | GPU Worker |
| Publisher | `providers.publisher().publish()` | Publisher Worker |

Прямі звернення до адаптерів (`ComfyUIAdapter()`, `TTSAdapter()`) у CORE також заборонені.

### 2.2 Control-plane / orchestration (дозволені в CORE)

Дозволені операції, що не є генерацією контенту:

- **FFmpeg assembly** — `providers.video_engine().render()` у `core/pipeline.py:finalize_job()`
  (AGENTS.md §20: «Не витрачати GPU на те, що нормально робиться звичайним монтажем»).
- **Config / metadata запити** — `providers.publisher().available_channels()`,
  `providers.publisher().configured()`, `provider_matrix()`.
- **Status reporting** — `provider_matrix()` для `/api/status` та Web UI.

### 2.3 LOCAL_WORKER_FALLBACK (задокументовані винятки)

Для single-node інсталяцій без окремого Worker-процесу існують локальні fallback-шляхи,
захищені змінною `LOCAL_WORKER_FALLBACK` та перевіркою наявності вузла
(`_has_text_worker()`, `_has_publisher_worker()`). Вони **тимчасові** і повинні зникнути
після того, як Worker завжди присутній (див. залежності §4).

## 3. Механізм gate

Статичний тест `tests/test_core_generation_gate.py`:

1. Сканує **всі** `core/*.py` файли рядок за рядком.
2. Шукає заборонені patterns (§2.1).
3. Кожне співпадіння, не внесене в `ALLOWLIST`, провалює тест-сьют.

`ALLOWLIST` у тесті містить задокументовані винятки (§2.2, §2.3) з прив'язкою
до конкретного файлу й номера рядка:

- `core/api/job_helpers.py:217` — ScriptAgent local fallback.
- `core/script_agent.py:21` — місце визначення LLM inference (виконується на Text Worker
  через `worker/role_executor.py`, не CORE orchestration).
- під час аудиту також задокументовано `core/pipeline.py:385` (assembly) та
  `core/app.py:848`, `core/pipeline.py:356` (publisher fallback). Оскільки ці ділянки
  не збігаються з literal-патернами gate (виклики через локальну змінну), вони не
  активують спрацювання, але внесені в audit-журнал (див. §5).

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

Стан `core/` після впровадження gate:

| Файл | Викликання | Класифікація | Дія |
|---|---|---|---|
| `core/pipeline.py:385` | `providers.video_engine().render()` | control-plane (assembly) | дозволено, AGENTS.md §20 |
| `core/pipeline.py:356` | `providers.publisher().publish()` (через `_publish_local`) | LOCAL_WORKER_FALLBACK | задокументовано, буде видалено |
| `core/app.py:848` | `providers.publisher().publish()` (Telegram callback) | LOCAL_WORKER_FALLBACK | задокументовано, буде видалено |
| `core/api/jobs.py:368` | `providers.publisher().available_channels()` | control-plane (metadata) | дозволено |
| `core/api/job_helpers.py:217` | `ScriptAgent().generate_script()` (local) | LOCAL_WORKER_FALLBACK | allowlist |
| `core/script_agent.py:21` | `get_llm_client().complete()` | LLM inference impl (Text Worker) | allowlist |
| `core/app.py` | `provider_matrix()` | control-plane (status) | дозволено |

**Висновок:** усі production generation stages у CORE оркеструють tasks і приймають
results. Прямі direct inference/synthesis/render/upload у CORE відсутні або явно
доведені як control-plane / задокументований fallback.