# Технічний аудит open-source рішень та оптимізація архітектури Vertep

> Статус: дослідження / підготовка рішення. Код не змінювався.
> Версія проєкту на момент аудиту: `0.0.0.x` (робоче дерево `main`).
> Дата: вересень 2026.

---

## 1. Executive Summary

Аудит підтвердив, що **базова архітектурна теза Vertep є правильною та виграшною**:
Vertep не генерує контент, а оркеструє зовнішні інструменти (Ollama, ComfyUI, FFmpeg, TTS,
Publisher, PostgreSQL, Redis, Grafana/Prometheus). Цей принцип уже фактично втілено в коді:
ядро (CORE) володіє повним життєвим циклом Job, диспетчеризує задачі за capabilities та VRAM,
через тонкі адаптери звертається до зовнішніх движків.

Висновок аудиту: **переписувати майже нічого не потрібно**. Основні компоненти, які
виконують «важку» роботу, вже делеговані зрілим open-source (ComfyUI — генерація зображень
і відео, Ollama — LLM, FFmpeg — монтаж, Redis + PostgreSQL — інфраструктура, Grafana +
Prometheus + Loki — моніторинг).

Що реально варто зробити на наступному етапі (без зміни принципів):
1. **Формалізувати Provider/Adapter шар** (LLM, Image, Video, TTS, Compute, Assembly,
   Publisher), щоб задні движки були взаємозамінними. Зараз адаптери є, але шар не
   уніфікований інтерфейсом.
2. **Інтегрувати готовий open-source TTS** (зараз `TTSAdapter` — це фактично заглушка
   `mock`/`none`).
3. **Додати OpenAI-сумісний LLM-провайдер** поверх Ollama, щоб зняти жорстку прив'язку
   до одного backend.
4. **Розглянути ComfyUI-Distributed як опційний ComputeProvider** за адаптером, а **не**
   як заміну власного диспетчера.
5. **Розглянути MoneyPrinterTurbo / ShortGPT як опційні engines** за `VideoEngine`
   інтерфейсом, зберігши власність життєвого циклу Job за Vertep.
6. **Реалізувати Publisher через офіційні API платформ** (зараз `publish()` — заглушка
   для всіх платформ, окрім Telegram і mock).

**Що залишається власним (KEEP)** як ключова цінність: Job Orchestrator і весь життєвий
цикл, GPU Dispatcher (job-level, VRAM/capability-aware), Node Registry, система персонажів,
безпека/оновлення (Signed Runtime, Update System), Web UI.

Рекомендація: **не переходити на готове рішення «цілком» (ані MoneyPrinterTurbo, ані
ShortGPT, ані ComfyUI-Distributed)**, а інтегрувати їх як зовнішні провайдери/движки за
універсальними інтерфейсами.

---

## 2. Current Architecture (фактична)

### 2.1 Загальний потік даних

```text
INPUT (Telegram / Manual Web / API / Scheduler)
   ↓
CORE (FastAPI: core/app.py)
   ├─ JobStore (core/pipeline.py) — зберігання та відновлення Job
   ├─ Job Orchestrator (core/orchestration.py) — стан-машина етапів і сцен
   ├─ Script Agent (core/script_agent.py) → LLM (Ollama)
   ├─ Queue (core/queue.py) — черга, inflight, dead-letter
   ├─ GPU Dispatcher (core/dispatcher.py) — вибір worker за VRAM/capability
   ├─ Node Registry (core/node_registry.py) — реєстрація/heartbeat вузлів
   └─ Repository (core/repository.py) — Postgres/файлове сховище
   ↓
Worker (worker/service.py) → role_executor (worker/role_executor.py)
   ├─ GPU: ComfyUIAdapter → ComfyUI (зовнішній сервіс)
   ├─ Text: Ollama
   ├─ Voice: TTS (заглушка)
   └─ Publisher / Backup: служби
   ↓
FFmpeg assembly (adapters/ffmpeg.py) → MP4
   ↓
Publisher (adapters/publisher.py) → Telegram (реально) / інші (заглушки)
```

### 2.2 Реалізовані шари

| Шар | Файли | Стан |
|---|---|---|
| CORE API | `core/app.py` (FastAPI, ~2500 рядків) | реалізовано |
| Job життєвий цикл | `core/models.py`, `core/orchestration.py`, `core/pipeline.py` | реалізовано (повний стан) |
| Script Agent | `core/script_agent.py`, `core/script_schema.py` | реалізовано (багатоступінчастий, fallback) |
| Queue | `core/queue.py` | реалізовано |
| Dispatcher | `core/dispatcher.py`, `services/dispatcher_service.py` | реалізовано (VRAM, capability, self-test) |
| Node Registry | `core/node_registry.py` | реалізовано (heartbeat 30 c, стани вузлів) |
| Worker | `worker/service.py`, `worker/role_executor.py` | реалізовано (roles, executors) |
| ComfyUI | `adapters/comfyui.py` | реалізовано (submit/wait/view, demo) |
| FFmpeg | `adapters/ffmpeg.py` | реалізовано (assembly, zoom, subtitles, watermark) |
| TTS | `adapters/tts.py` | **заглушка** (`mock`/`none`) |
| LLM | `adapters/llm.py`, `core/script_agent.py` | Ollama-only (жорстка прив'язка) |
| Publisher | `adapters/publisher.py` | Telegram реальний; інші — заглушки |
| Telegram | `adapters/telegram.py` | реалізовано |
| Інфраструктура | Redis, PostgreSQL, Grafana/Prometheus/Loki | зовнішні сервіси (вже інтегровані) |
| Update/Безпека | `core/update_*`, `scripts/release*`, `installer/` | реалізовано (Signed Runtime) |
| Web UI | `web`, `web-v2` (Angular) | реалізовано |
| Ролі | `config/node_roles.json` | реалізовано (7 ролей) |

### 2.3 Сторонні движки вже використовуються

- **Ollama** — LLM (через HTTP `/api/generate`).
- **ComfyUI** — генерація зображень/відео (через HTTP API як чорний ящик).
- **FFmpeg** — монтаж відео.
- **PostgreSQL** — стан, журнали, реєстр вузлів, міграції.
- **Redis** — черги/кеш.
- **Grafana + Prometheus + Loki** — моніторинг.

Це вже відповідає принципу «зовнішні движки за адаптерами». Винятки — TTS і Publisher
реальної генерації/публікації, які поки є заглушками.

---

## 3. Existing Solutions (знайдені open-source аналоги)

| Проєкт | Посилання | Ліцензія | Зірки | Активність | Чим корисний для Vertep |
|---|---|---|---|---|---|
| ComfyUI | `github.com/Comfy-Org/ComfyUI` | GPL-3.0 | ~132k | дуже активний | Генерація зображень/відео через HTTP API (вже інтегрований). Чорний ящик — не копіювати код. |
| MoneyPrinterTurbo | `github.com/harry0703/MoneyPrinterTurbo` | MIT | ~121k | дуже активний | topic→script→TTS→субтитри→музика→assembly. Референс для Script/TTS/Assembly і провідників. Залежить від cloud API (Kimi/OpenAI, Azure/Edge TTS). |
| ShortGPT | `github.com/RayVentura/ShortGPT` | MIT | ~7.9k | неактивний (лют. 2025, «experimental») | Engines: ContentShortEngine/ContentVideoEngine/ContentTranslationEngine. Референс для архітектури engine. Залежить від OpenAI/ElevenLabs/EdgeTTS/Pexels. |
| ComfyUI-Distributed | `github.com/robertvoy/ComfyUI-Distributed` | Apache-2.0 | ~619 | активний (23 дні тому) | Розподіл ComfyUI-роботи між GPU локально/хмарою. Опційний ComputeProvider. Не комбінує VRAM, не прискорює одинарну генерацію. |
| Ollama | `github.com/ollama/ollama` | MIT | зрілий | активний | Локальний LLM (вже використовується). |
| vLLM / llama.cpp | `github.com/vllm-project/vllm` / `ggml-org/llama.cpp` | Apache-2.0 / MIT | зрілі | активні | Альтернативні OpenAI-сумісні LLM сервери (за LLMProvider). |
| Piper | `github.com/rhasspy/piper` | MIT | зрілий | активний | Локальний нейро-TTS, комерційно безпечна ліцензія. |
| Kokoro-TTS | `github.com/hexgrad/kokoro` | Apache-2.0 | активний | активний | Високоякісний локальний TTS, Apache-2.0. |
| Edge-TTS | `github.com/rany2/edge-tts` | GPL-3.0(прим.) | зрілий | активний | Безкоштовний TTS Microsoft, але неофіційний API + ліцензійні/ToS ризики. |
| Coqui XTTS | `github.com/coqui-ai/TTS` | CPML (non-commercial) | зрілий | помірно активний | Хороша якість, але **обмеження на комерційне використання**. |
| Celery / RQ / Dramatiq | — | BSD / MIT / LGPL | зрілі | активні | Універсальні job queues (альтернатива власній `queue.py`). |
| n8n / Temporal / Prefect | — | Sustainable / MIT | зрілі | активні | Workflow-оркестрація (референс; не замінює Vertep Job). |
| Grafana + Prometheus + Loki | — | AGPL / Apache | зрілі | активні | Вже інтегровані для моніторингу. |

> **Зауваження щодо «AI Content Factory»**: під цією назвою в аудиті зрозуміло цілий клас
> проєктів автогенерації короткого відео (money/short/faceless factories). Найзріліші із них —
> MoneyPrinterTurbo (MIT) і ShortGPT (MIT). Інші аналоги дрібніші, часто залежні від одного
> cloud API і неактивні. Для Vertep вони є джерелами ідей та опційними engines, а не
> заміною ядра.

---

## 4. Component Matrix

Позначення рішень: **KEEP** (власне), **REPLACE** (замінити), **INTEGRATE** (підключити
зовнішній компонент через adapter), **REUSE** (використати ідеї/код за умов ліцензії),
**REMOVE** (власна реалізація більше не потрібна), **REFACTOR** (залишити, але перейти на
універсальний інтерфейс).

| Компонент Vertep | Поточний стан | Готове рішення | Рішення | Обґрунтування |
|---|---|---|---|---|
| **Job Orchestrator** (життєвий цикл, стан-машина) | реалізовано (`core/orchestration.py`) | — | **KEEP** | Ключова цінність Vertep. Жоден зовнішній проєкт не володіє станом мульти-етапного Job так, як потрібно. |
| **GPU Dispatcher** (вибір worker за VRAM/capability, heartbeat, retry) | реалізовано (`core/dispatcher.py`, `node_registry.py`) | ComfyUI-Distributed (частково) | **KEEP** (+ опційно INTEGRATE) | Диспетчер вже покриває реєстрацію, heartbeat, VRAM, self-test, retry, failover. ComfyUI-Distributed не дає job queue/VRAM-планер на рівні Vertep. |
| **Worker (node agent)** | реалізовано (`worker/service.py`, `role_executor.py`) | — | **KEEP** | Специфічний протокол з CORE, self-test, update coordination. |
| **ComfyUI executor** | реалізовано (`adapters/comfyui.py`) | ComfyUI (зовнішній) + ComfyUI-Distributed | **REFACTOR** → `ImageProvider`/`VideoProvider` | Залишити ComfyUI як зовнішній GPL-сервіс (чорний ящик). Винести за інтерфейс, щоб можна було підключити інший backend. |
| **FFmpeg assembly** | реалізовано (`adapters/ffmpeg.py`) | MoneyPrinterTurbo/ShortGPT (assembly ідеї, MIT) | **KEEP** (+ optional REUSE) | Покриття вже хороше (zoom, subtitles, watermark, music). Можна покращити деталями з MIT проєктів. |
| **TTS** | заглушка (`mock`/`none`) | Piper (MIT), Kokoro (Apache-2.0), Edge-TTS, Coqui | **INTEGRATE** | Прибрати власну заглушку `mock`, підключити готовий локальний нейро-TTS через `TTSProvider`. Це найбільша «економія» від аудиту. |
| **LLM провайдер** | Ollama-only (жорстко в `script_agent.py`, `llm.py`) | vLLM/llama.cpp (OpenAI-сумісні) | **REFACTOR** → `LLMProvider` | Додати OpenAI-сумісний інтерфейс. Ollama залишається дефолтом. |
| **Script Agent** | реалізовано (`core/script_agent.py`) | MoneyPrinterTurbo (script-логіка, MIT) | **KEEP** (логіка) + REFACTOR (L/T provider) | Багатоступінчастий сценарний агент із власним fallback — вже добре. Під`єднати до `LLMProvider`. |
| **Publisher** | Telegram реальний; інші — заглушки | офіційні API YouTube/TikTok/FB/IG | **KEEP** (ядро) + INTEGRATE (офіційні API) | Реалізувати live-адаптери через офіційні API/OAuth. Залишити чергування/retry/історію в Vertep. |
| **Queue** | реалізовано (`core/queue.py`) | Celery/RQ/Dramatiq | **KEEP** | Власна черга достатня, орієнтована на життєвий цикл Vertep. Не вводити нову залежність. |
| **Character System** (конфіги) | реалізовано (`characters/<id>/`) | — | **KEEP** | Незалежна сутність. Не переносити в зовнішні движки. |
| **Content Sources** (Telegram) | реалізовано (`adapters/telegram.py`) | — | **KEEP** | Перше джерело — Telegram. Модульний підхід уже правильний. |
| **Web UI** | реалізовано (`web`, `web-v2` Angular) | — | **KEEP** | Власний, локалізований, інтегрований із API. |
| **Installer / Deployment Wizard** | реалізовано (`installer/`, `bootstrap.sh`) | — | **KEEP** | Appliance-specific, Signed Runtime. |
| **Update System / Security** | реалізовано (`core/update_*`, `scripts/release*`) | — | **KEEP** | Ключова цінність і безпековий бар'єр. |
| **Node Registry / Roles** | реалізовано (`node_registry.py`, `node_roles.json`) | — | **KEEP** | Capability-driven архітектура — вже сучасна. |
| **DB / Queue інфраструктура** | PostgreSQL + Redis | зовнішні (вже) | **INTEGRATE** (вже) | Зберегти як зовнішні сервіси. |
| **Monitoring** | Grafana + Prometheus + Loki | зовнішні (вже) | **INTEGRATE** (вже) | Готовий стек вже інтегрований. |
| **License Manager / Backup** | власні служби | — | **KEEP** | Комерційно-проприєтарна логіка. |

---

## 5. Recommended Architecture

### 5.1 Цільова схема (Mermaid)

```mermaid
flowchart TB
    subgraph Sources
        TG[Telegram]
        WEB[Web UI / Manual]
        API[API / Scheduler]
    end

    TG --> CORE
    WEB --> CORE
    API --> CORE

    subgraph "Vertep CORE"
        CORE[Mono/FastAPI]
        CH[Character Engine]
        JO[Job Orchestrator]
        REG[Node Registry]
        DISP[Dispatch + Scheduler]
        CORE --> CH
        CORE --> JO
        JO --> REG
        JO --> DISP
    end

    subgraph "Provider Layer (interfaces)"
        LLMP[LLMProvider]
        IMGP[ImageProvider]
        VIDP[VideoProvider]
        TTSP[TTSProvider]
        ASMP[AssemblyProvider]
        COMP[ComputeProvider]
        PUBP[PublisherProvider]
    end

    JO --> LLMP
    JO --> IMGP
    JO --> VIDP
    JO --> TTSP
    JO --> ASMP
    DISP --> COMP
    JO --> PUBP

    subgraph "External Engines"
        OLLAMA[Ollama / vLLM]
        COMFY[ComfyUI (GPL black-box)]
        VENG[VideoEngine: NativeVertep / MoneyPrinter / ShortGPT]
        FF[FFmpeg]
        COMDF[ComfyUI-Distributed (optional)]
        PLAT[YouTube / TikTok / FB / IG / TG]
    end

    LLMP --> OLLAMA
    IMGP --> COMFY
    VIDP --> VENG
    VIDP --> COMFY
    TTSP --> COMFY
    ASMP --> FF
    COMP --> COMFY
    COMP --> COMDF
    PUBP --> PLAT
```

### 5.2 Пояснення до схеми

- **Provider Layer** — це формалізація наявних `adapters/` у спільні інтерфейси
  (`LLMProvider`, `ImageProvider`, `VideoProvider`, `TTSProvider`, `AssemblyProvider`,
  `ComputeProvider`, `PublisherProvider`). Кожен інтерфейс має конкретну реалізацію
  (напр. `ComfyUIProvider`, `OllamaProvider`, `PiperProvider`, `VertepWorkerProvider`).
- **Job Orchestrator** залишається єдиним власником стану Job. Зовнішній движок
  (ComfyUI, MoneyPrinterTurbo тощо) виконує окремий етап і повертає артефакти; він **не**
  володіє життєвим циклом.
- **ComputeProvider** інкапсулює «де саме рахується задача»:
  - `VertepWorkerProvider` (поточна реалізація: Worker → ComfyUI) — дефолт;
  - `ComfyUIDistributedProvider` (опційно) — якщо треба розкласти генерацію на кілька GPU
    одного кластера без зміни CORE;
  - майбутні кластерні backend.
- **VideoEngine** (не плутати з `VideoProvider`) — високорівневий «драйвер збірки»:
  `NativeVertepEngine` (поточний FFmpeg-пайплайн) і, опційно, `MoneyPrinterEngine` /
  `ShortGPTEngine`. Vertep залишає за собою стан, персонажа, метадані; движок лише
  виконує збірку.
- **Character Engine** залишається незалежним: персонаж формує prompt/voice/visual/
  publishing-налаштування, але не переноситься всередину зовнішніх компонентів.

### 5.3 Правило не-залежності

```text
Vertep CORE  ──►  Provider Interface  ──►  External Engine
```

Жоден шар не повинен імпортувати/сприймати конкретний зовнішній проєкт як обов'язковий.
Якщо `ComfyUIProvider` можна замінити на `OtherProvider` без зміни `Job Orchestrator` —
архітектура досягла мети.

---

## 6. Dependency Risks

| Ризик | Джерело | Мітигація |
|---|---|---|
| **GPL-3.0 «зараження»** | ComfyUI | Використовувати виключно як зовнішній HTTP-сервіс (чорний ящик). **Не** копіювати код ComfyUI у Vertep. Ліцензійна межа «процес/мережа» не втягує GPL у проприєтарний код. |
| Прив'язка до одного LLM backend | зовнішня (Ollama) | `LLMProvider` + OpenAI-сумісний інтерфейс; Ollama — дефолт, не єдиний. |
| Прив'язка до ComfyUI | зовнішня | `ImageProvider`/`VideoProvider`/`ComputeProvider`; дефолт — ComfyUI. |
| TTS-ліцензії (не-комерційні) | Coqui CPML, Edge-TTS GPL | Для production вибрати Piper (MIT) або Kokoro (Apache-2.0). Не брати non-commercial. |
| Неактивність ShortGPT | ShortGPT (лют. 2025, experimental) | Не вводити як обов'язкову залежність; лише референс або опційний engine під контролем Vertep. |
| Cloud-залежності copycat-проєктів | MoneyPrinterTurbo (Kimi/OpenAI/Azure), ShortGPT (OpenAI/ElevenLabs) | Не наслідувати їхню залежність від платних API; у Vertep пріоритет — локальні движки. |
| ComfyUI-Distributed обмеження | не комбінує VRAM, не прискорює одиничну генерацію | Використовувати лише як опційний ComputeProvider для паралельних задач, не покладатися на нього для одного важкого Job. |
| Vendor lock платформ публікації | YouTube/TikTok/FB | Використовувати офіційні API, але тримати всю логіку чергування/історії в Vertep (PublisherProvider). |
| Оновлення/безпека як єдине джерело | власне | Залишити проприєтарною (KEEP) — зовнішні проєкти не дають зрілого Signed Update для даного appliance. |

---

## 7. License Analysis

| Проєкт | Ліцензія | Можна використовувати? | Умови |
|---|---|---|---|
| ComfyUI | GPL-3.0 | Так, тільки як зовнішній сервіс | Тримати за HTTP-межею (чорний ящик). Не включати код у Vertep. |
| ComfyUI-Distributed | Apache-2.0 | Так (permissive) | Можна інтегрувати як ComputeProvider, зберігати атрибуції. |
| MoneyPrinterTurbo | MIT | Так (permissive) | Можна копіювати/адаптувати з атрибуцією; залежності від cloud API — на розсуд. |
| ShortGPT | MIT | Так (permissive) | Референс/опційний engine, з атрибуцією; проєкт неактивний. |
| Ollama | MIT | Так | Вже використовується. |
| vLLM | Apache-2.0 | Так | Опційний LLM-сервер. |
| llama.cpp | MIT | Так | Опційний LLM-сервер. |
| Piper | MIT | Так (комерційно безпечно) | Рекомендований локальний TTS. |
| Kokoro-TTS | Apache-2.0 | Так (комерційно безпечно) | Якісний локальний TTS. |
| Edge-TTS | GPL-3.0 (прим.), неофіційний API | Обережно | Неофіційний Microsoft API; ризики ToS. Для dev-режиму лише. |
| Coqui XTTS | CPML (non-commercial) | Тільки non-commercial | **Не** використовувати в production комерційному. |
| Celery / RQ / Dramatiq | BSD / MIT / LGPL | Так | За потреби як альтернатива черзі. |
| n8n / Prefect / Temporal | Sustainable / MIT / MIT | Так | Референс для workflow-оркестрації. |
| Grafana | AGPL | Так (зовнішній сервіс) | Вже інтегрований як зовнішній. |
| Prometheus / Loki | Apache-2.0 | Так | Вже інтегровані. |

> Правило: **пермісівні ліцензії (MIT/Apache-2.0/BSD)** — можна інтегрувати та копіювати;
> **GPL/AGPL** — використовувати лише як зовнішні процеси, не зв'язувати з власним кодом;
> **CPML/non-commercial** — не брати для комерційного Vertep.

---

## 8. Migration Plan

План поетапний. Кожен етап — окремий версійний commit за правилами Vertep (пуш/реліз),
з повним тестовим проходом. Етапи впроваджувати лише після окремого підтвердження.

### Фаза 0 — Цей аудит (без змін коду)
- Створено `docs/architecture/open-source-audit.md`.
- Ничого в коді не змінювалося.

### Фаза 1 — Формалізація Provider Layer (REFACTOR)
- Визначити інтерфейси `LLMProvider`, `ImageProvider`, `VideoProvider`, `TTSProvider`,
  `AssemblyProvider`, `ComputeProvider`, `PublisherProvider`.
- Обгорнути наявні адаптери (`ComfyUIAdapter`, `LLMAdapter`, `TTSAdapter`, `FFmpegAdapter`,
  `Publisher`) як дефолтні реалізації.
- Оновити `pipeline.py`/`role_executor.py` на роботу через інтерфейси (з тим самим
  дефолтним backend).
- Тести: повний прогон; поведінка не змінюється.

### Фаза 2 — LLM OpenAI-сумісний провайдер + TTS (INTEGRATE)
- `OllamaProvider` (дефолт) + `OpenAICompatProvider` (fleet). Вибір через конфіг/`.env`.
- `TTSProvider` → реалізація `PiperProvider` (MIT) та/або `KokoroProvider` (Apache-2.0);
  видалити заглушку `mock`/`none`.
- Тести: LLM-перемикання, реальний синтез WAV.

**Виконано:**
- `adapters/llm_clients.py`: `OllamaClient` (дефолт, `/api/generate`) та `OpenAICompatClient`
  (fleet, `/v1/chat/completions` з підтримкою `response_format` і fallback-ретраєм);
  селектор `get_llm_client()` через `VERTEP_LLM_PROVIDER` (`ollama` | `openai`).
  Назва клієнтів (`*Client`, а не `*Provider`) обрана тому, що вони віддають один
  raw-completion-контракт `complete()`, а не повний сценарій.
- `core/script_agent.py`: `ScriptAgent` тепер ходить через `get_llm_client()` замість
  прямого `httpx`-виклику Ollama; backend-агностичний.
- `adapters/providers/tts_backends.py`: `PiperProvider` (MIT, CLI та HTTP-режим) і
  `KokoroProvider` (Apache-2.0, OpenAI-сумісний `/v1/audio/speech`), обидва за
  інтерфейсом `TTSProvider`.
- `adapters/providers/__init__.py`: фабрика `_make_tts()` маршрутизує імена `piper` /
  `kokoro` на нові движки, решта — на `TTSAdapter`.
- Тести: `tests/test_llm_tts_providers.py` (вибір backend, парсинг запитів Ollama /
  OpenAI, синтез WAV Piper CLI+HTTP та Kokoro, маршрутизація registry, підключення
  `ScriptAgent`).

**Відхилення від початкового плану:**
- `mock`/`none` заглушки **збережено** (не видалено). Вони використовуються тестами
  Phase 1 (`tests/test_providers.py`, `tests/test_local_contracts.py`) та demo-режимом;
  повне їх видалення зламало б наявний тестовий контракт. Нові живі движки
  (`piper`/`kokoro`) увімкнено через конфіг без зміни дефолтного тестового шляху.

### Фаза 3 — Оновлення Publisher live-адаптерів (INTEGRATE) ✅
- Реалізовано офіційні API/OAuth для YouTube/TikTok/Facebook/Instagram/Threads за
  `PublisherProvider`; Telegram вже є.
- Адаптери перенесено в `publishers/<platform>/` (шаблон AGENTS) із власним
  тестованим HTTP-транспортом (`publishers/transport.py`): `HttpTransport` для
  реальних викликів і `FakeTransport` для тестів.
- `publishers/base.py` — спільна база `Publisher`: конфігурація
  (`PUBLISHER_MOCK` або платформовий credential env), обробка помилок → `FAILED`,
  mock-режим для тестів/demo (збережено, як у Phase 2).
- `adapters/publisher.py` — агрегатор (`PUBLISHERS` + `TelegramChannelPublisher`),
  зберігає контракт для `DefaultPublisherProvider` і наявних тестів.

Потоки live:
- **YouTube** — resumable upload (init → PUT байтів), `YOUTUBE_ACCESS_TOKEN`.
- **Facebook** — Graph API `/{page}/videos` multipart, `FACEBOOK_ACCESS_TOKEN` +
  `FACEBOOK_PAGE_ID`.
- **Instagram** — Graph API Reels two-step (media container → `media_publish`),
  потребує хостований `metadata["video_url"]`.
- **Threads** — Threads API two-step (`threads` → `threads_publish`), потребує
  хостований `metadata["video_url"]`.
- **TikTok** — Content Posting API three-step (init → PUT → status fetch),
  `TIKTOK_ACCESS_TOKEN` (+ опційний `TIKTOK_X_TT_POST`).

Тести: `tests/test_publisher_live_adapters.py` — mock-транспорт для кожного
потоку, обробка помилок HTTP, відсутніх конфігів та інтеграційна маршрутизація
`DefaultPublisherProvider` до live-адаптера.

### Фаза 4 — Опційний ComputeProvider (INTEGRATE) ✅
- `ComfyUIDistributedProvider` за `ComputeProvider` (опційно, окрема конфігурація).
- Дефолт залишається `VertepWorkerProvider`; кластер — лише за явного включення.
- Тести: ізоляція, fallback на VertepWorker.

**Реалізовано (Phase 4):**
- `adapters/providers/compute_backends.py` — `ComfyUIDistributedProvider(ComputeProvider)`
  з тим самим контрактом `generate_output(workflow, topic, task_type) -> (bytes, filename, kind)`
  та `cancel()`. Надсилає workflow на proxy `COMFYUI_DISTRIBUTED_URL` (`/prompt`), опитує
  `/history/<id>`, завантажує артефакт через `/view`; опційний Bearer-токен
  (`COMFYUI_DISTRIBUTED_TOKEN`). HTTP ходить через інʼєкційний `HttpTransport`
  (як у Phase 3), тому тестується через `FakeTransport` без мережі.
- `adapters/providers/__init__.py` — `_make_compute()`: дефолт залишається
  `DefaultComputeProvider(ComfyUIAdapter)` (VertepWorker); кластер умикається лише за
  `VERTEP_COMPUTE_PROVIDER=comfyui-distributed` **і** налаштованого `COMFYUI_DISTRIBUTED_URL`;
  інакше — fallback на VertepWorker. `ComfyUIDistributedProvider` експортовано.
- `.env.example` — `VERTEP_COMPUTE_PROVIDER`, `COMFYUI_DISTRIBUTED_URL`,
  `COMFYUI_DISTRIBUTED_TOKEN`, polling-таймінги.
- `tests/test_compute_distributed.py` — ізоляція (без URL → не налаштований), fallback на
  VertepWorker, opt-in при налаштованому URL, image/video HTTP-потоки, Bearer-токен,
  HTTP-помилка → `HTTP <code>`, `cancel()` на `/interrupt`, swap у registry.
- `publishers/transport.py` — додано `get()` до `HttpTransport` (вже був у `FakeTransport`).

**Відхилення від початкового плану:**
- Дефолтні тестові контракти (Phase 1) та самотест worker (`worker/service.py`) продовжують
  використовувати `providers.compute()` (VertepWorker). `ComfyUIDistributedProvider` не
  впливає на них, бо factory за замовчуванням повертає VertepWorker.

### Фаза 5 — VideoEngine pattern (REUSE, опційно) ✅
- `NativeVertepEngine` (поточний FFmpeg-пайплайн) як дефолт.
- Опційно `MoneyPrinterEngine`/`ShortGPTEngine` за `VideoEngine` інтерфейсом.
- Життєвий цикл Job і персонаж залишаються в Vertep.
- Тести: parity «Native» vs «зовнішній» на одному Job.

**Реалізовано (Phase 5):**
- `adapters/providers/base.py` — додано ABC `VideoEngine` з контрактом
  `render(output, *, images, clips, durations, audio, music, subtitles, aspect_ratio,
  preset, watermark, task_type) -> Path`. Це високорівневий «драйвер збірки»,
  відмінний від `VideoProvider` (AI-генерація кліпів) та `AssemblyProvider`
  (низькорівневі примітиви FFmpeg). Жоден зовнішній движок не може змінити
  стан Job чи персонажа — Vertep залишає за собою цикл.
- `adapters/providers/video_engines.py` — реалізації:
  - `NativeVertepEngine(VideoEngine)` — дефолт; оборачує `AssemblyProvider`
    (FFmpegAdapter). Для `task_type == "video"` → `assemble_clips()`; інакше →
    `assemble()` з zoom/fades/music/watermark. Тести використовують реальний
    FFmpeg (imageio_ffmpeg) для рендеру mp4 з PPM-зображень.
  - `RemoteVideoEngine(VideoEngine)` — абстрактний базовий клас для
    HTTP-рушіїв: `POST /render` → `GET /jobs/<id>` (poll) → `GET /download/<id>`.
    Ані-які мережеві виклики проходять через інʼєкційний `HttpTransport` —
    тестується через `FakeTransport` без мережі.
  - `MoneyPrinterEngine(RemoteVideoEngine)` / `ShortGPTEngine(RemoteVideoEngine)`
    — іменовані variants, що беруть URL та токен з відповідних env
    (`MONEY_PRINTER_URL`/`SHORTGPT_URL`, `MONEY_PRINTER_TOKEN`/`SHORTGPT_TOKEN`).
- `adapters/providers/_http.py` — спільний `check_response()` для HTTP-помилок,
  що використовується як `compute_backends.py`, так і `video_engines.py`, щоб
  уникнути 4-кратної дублікації. `publishers/base.check_response()` залишається
  окремим (інший пакет).
- `adapters/providers/__init__.py` — `_make_video_engine()` фабрика: дефолт
  `NativeVertepEngine(DefaultAssemblyProvider(FFmpegAdapter()))`; зовнішні движки
  умикаються лише за `VERTEP_VIDEO_ENGINE=money-printer|shortgpt` **і**
  налаштованого URL; інакше — fallback на native. Доступ `video_engine()` до
  ProviderRegistry, реєстрація в `_create_default_registry()`, експорт ABC та
  implementations у `__all__`.
- `core/pipeline.py` — `finalize_job()` тепер викликає
  `providers.video_engine().render(...)` замість прямих `providers.assembly()`
  викликів; `concat_audio()` для voice-concat продовжує використовувати
  `providers.assembly()`. Job-цикл, персонаж, metadata залишаються в Vertep.
- `.env.example` — `VERTEP_VIDEO_ENGINE`, `MONEY_PRINTER_URL`,
  `MONEY_PRINTER_TOKEN`, `SHORTGPT_URL`, `SHORTGPT_TOKEN`, polling-таймінги.
- `tests/test_video_engines.py` — 11 тестів:
  - фабрика: default → native; fallback без URL → native; opt-in
    money-printer; opt-in shortgpt;
  - native engine рендерить реальний mp4 з PPM-зображення (FFmpeg);
  - remote engine isolation (без URL → RuntimeError);
  - HTTP-потік через FakeTransport: POST /render → poll → GET /download →
    output; правильні spec (images, durations, aspect_ratio, task_type);
  - Bearer-токен відправляється; HTTP-помилка → `RuntimeError` з кодом;
  - **parity**: Native та external на одному Job — однакові asset inputs,
    обидва створюють output файл;
  - swap у registry.

**Додаткові зміни:**
- `adapters/providers/compute_backends.py` — прибрано локальну дублікацію
  `_check()`; замінено на `from ._http import check_response as _check`.
- Файл змінено: `adapters/providers/base.py`, `adapters/providers/__init__.py`,
  `adapters/providers/video_engines.py`, `adapters/providers/_http.py`,
  `adapters/providers/compute_backends.py`, `core/pipeline.py`,
  `tests/test_video_engines.py`, `.env.example`,
  `docs/architecture/open-source-audit.md`.

### Фаза 6 — Документація та hardening ✅
- Оновити `AGENTS.md` і README під нові Provider-інтерфейси.
- Додати матрицю підтримуваних backend у Web UI.

**Реалізовано (Phase 6):**
- `adapters/providers/__init__.py` — додано `provider_matrix()`: описує активні
  backend для всіх provider-слотів (`llm`, `tts`, `compute`, `image`, `video`,
  `assembly`, `video_engine`, `publisher`). Для кожного слота повертає активний
  backend, доступні альтернативи, керівну env-змінну та стан `configured`;
  для публікатора — per-platform прапори. Усі перевірки локальні (без HTTP).
  Функцію додано в `__all__`.
- `core/app.py` — `/api/status` тепер повертає поле `providers` із
  `provider_matrix()`, що живить Web UI.
- `web-v2` — у Settings додано панель **«Движки обробки (backends)»**: таблиця
  активних backend для кожної ролі (LLM, TTS, GPU-обчислення, image/video,
  монтаж, движок збірки, публікація) зі станом «налаштовано / не налаштовано».
  У `core/models.ts` додано інтерфейси `ProviderSlot`.
- `AGENTS.md` — розділ 28 «Provider / Adapter Layer»: інтерфейси, фабрики та
  registry, матриця доступних задніх движків із env, правила використання.
- `README.md` — розділ «Provider layer (replaceable engines)»: опис
  інтерфейсів, опційних движків та посилання на матрицю в Web UI.
- `tests/test_provider_matrix.py` — 7 тестів: дефолти, перемикання LLM на
  OpenAI + вимога ключа, fallback ComfyUI-Distributed без URL, активний
  comfyui-distributed з URL, перемикання VideoEngine, TTS backend,
  publisher-платформи.

**Файли змінено:** `adapters/providers/__init__.py`, `core/app.py`,
`web-v2/src/app/core/models.ts`, `web-v2/src/app/settings/settings.component.ts`,
`AGENTS.md`, `README.md`, `tests/test_provider_matrix.py`,
`docs/architecture/open-source-audit.md`.

---

## 9. Development Savings

Від розробки яких компонентів можна **відмовитися / не розробляти самому**:

| Компонент | Економія | Джерело |
|---|---|---|
| **TTS движок** | Не писати власний нейро-TTS. Підключити Piper (MIT) / Kokoro (Apache-2.0). | найбільша економія |
| **LLM serving / інференс** | Не писати сервер LLM. Використати Ollama / vLLM. | вже зроблено (Ollama) |
| **Diffusion (image/video) backend** | Не писати генератор. Використати ComfyUI (зовнішній). | вже зроблено |
| **Distributed GPU compute** | Не писати з нуля. Опційно ComfyUI-Distributed (Apache-2.0). | за потреби |
| **FFmpeg assembly деталі** | Не будувати монтаж з нуля — розширити поточний `FFmpegAdapter` ідеями з MIT проєктів. | MoneyPrinterTurbo/ShortGPT |
| **Сценарна генерація (pattern)** | Референс для model/script-структури (не код-копія). | MoneyPrinterTurbo |
| **Моніторинг** | Вже інтегрований Grafana/Prometheus/Loki. | вже зроблено |
| **DB/черги** | Вже Redis + PostgreSQL. | вже зроблено |
| **Publishing** | Не будувати неофіційні інтеграції; використати офіційні платформні API/SDK. | офіційні SDK |

**Чого не економити (залишити власним):** Job Orchestrator, Dispatcher (job-level),
Node Registry, Character System, Update/Security/Release, Web UI, Installer.

---

## 10. Final Recommendation

1. **Залишити Vertep оркестратором верхнього рівня** — жоден зовнішній проєкт (ані
   MoneyPrinterTurbo, ані ShortGPT, ані ComfyUI-Distributed) не замінює ядро.
2. **Формалізувати Provider/Adapter шар** — це головний рефакторинг, що робить задні
   движки взаємозамінними без зміни Job Orchestrator.
3. **Інтегрувати готовий локальний TTS** (Piper / Kokoro) замість заглушки — найбільша
   швидка вигода.
4. **Додати OpenAI-сумісний LLM-провайдер**, залишивши Ollama дефолтом.
5. **Reuse, не replace**: використовувати MoneyPrinterTurbo/ShortGPT як референс та опційні
   `VideoEngine`, а не як заміну.
6. **ComfyUI-Distributed — опційний ComputeProvider**, не заміна Dispatcher.
7. **Керувати ліцензійними межами**: ComfyUI (GPL) — лише зовнішній сервіс; TTS — лише
   пермісівні ліцензії.
8. **Реалізувати Publisher** через офіційні API, зберігши життєвий цикл і історію в Vertep.

**Підсумок**: архітектура Vertep вже близька до цільової. Аудит не виявив потреби в
кардинальній перебудові — лише в уніфікації інтерфейсів (provider/adapter) та підключенні
кількох конкретних open-source компонентів (TTS, LLM provider, опційно distributed compute
і альтернативні video engines).

---

## Appendix A — GPU Dispatcher: детальний аналіз

### Поточний стан Vertep (`core/dispatcher.py`, `core/node_registry.py`, `worker/service.py`)
- Worker реєструється у CORE з capabilities, hardware (VRAM, GPU name, compute capability),
  heartbeat кожні ~30 c, self-test (attested capabilities).
- `available_worker()`: фільтр за `last_seen ≤ 45 c`, статус `ONLINE/FREE/READY`,
  обов'язковий свіжий self-test (опційно), `free_vram_mb ≥ min_vram_mb`, capability
  (`image_generation`, `video_generation`, ...), supported workflows; сортування за
  priority/load/free-VRAM.
- Підтримує різні GPU (NVIDIA/AMD), `gpu_profiles.py` (Turing/Pascal/Ampere/Hopper),
  `--lowvram` для ≤8 GB.

### Порівняння з ComfyUI-Distributed (`robertvoy/ComfyUI-Distributed`, Apache-2.0)

| Можливість | Vertep Dispatcher | ComfyUI-Distributed |
|---|---|---|
| Реєстрація workers | Так (Node Registry + self-test) | Частково (auto-setup/master-workers, без керування станами ролей) |
| Heartbeat / стан | Так (30 c, стани ONLINE/BUSY/...) | Частково (підключення/відключення воркерів) |
| Визначення GPU / VRAM | Так (VRAM, load, temp, compute cap) | Частково (визначає наявність, але не планує за VRAM на рівні Vertep) |
| VRAM-aware scheduling | Так (min_vram_mb, free_vram) | Ні (не комбінує VRAM; одне завдання на воркер) |
| Job queue / retries | Так (`queue.py`, `can_retry`) | Ні (це процесор-розширювач, не job queue) |
| Failure recovery | Так (рестарт/відновлення Job) | Частково (fallback на master) |
| Різні GPU разом | Так | Так, але з дисбалансом, якщо GPU дуже різні |
| Одночасні паралельні задачі | Так (різні воркери) | Так (розподіляє роботу між GPU) |

**Висновок**: Vertep Dispatcher — повноцінний job-level планувальник. ComfyUI-Distributed
— це низькорівневий «розкладач роботи всередині одного майстра ComfyUI кластера». Вони
доповнюють, а не замінюють одне одного.

**Рекомендація**: тримати Vertep Dispatcher (KEEP). Якщо з'явиться потреба розкласти
роботу на кілька GPU одного логічного воркера — підключити `ComfyUIDistributedProvider`
за `ComputeProvider` (INTEGRATE) у координації з Vertep Dispatcher через адаптер
`Vertep Dispatcher → ComputeProvider → ComfyUI-Distributed`. Не замінювати Dispatcher.

---

## Appendix B — Генерація відео: порівняння pipeline

| Функція | Vertep (поточний) | MoneyPrinterTurbo | ShortGPT |
|---|---|---|---|
| topic→script | Так (ScriptAgent, багатоступінчастий) | Так (LLM) | Так (OpenAI) |
| scene planning | Так (scenes_plan, per-scene prompts) | Так (segments) | Так (Content engines) |
| assets | ComfyUI (згенеровані зображення) | сток-відео (Pexels) | сток (Pexels/Bing) |
| TTS | **заглушка** | Edge/Azure/ElevenLabs | EdgeTTS/ElevenLabs |
| subtitles | Так (SRT, file-based) | Так (edge/whisper + burned) | Так (timing) |
| music | Так (опційно, amix) | Так | Ні (базово) |
| FFmpeg assembly | Так (zoompan, fades, watermark) | Так (MoviePy) | MoviePy |
| metadata | Так (title/desc/hashtags/platforms) | Так | Так (YouTube metadata) |
| publishing | Telegram реально, інші заглушки | частково | частково |
| Головний generator | приватний (ComfyUI) | сток-футаж | сток-футаж |

**Висновок**: Vertep-пайплайн функціонально повніший за ShortGPT і співставний із
MoneyPrinterTurbo, з тією ключовою відмінністю, що Vertep генерує візуал через
приватний ComfyUI, а не через сток. Найбільший реальний розрив — **TTS** (заглушка).
Використовувати MoneyPrinterTurbo/ShortGPT як **engines behind `VideoEngineInterface`**
(наприклад, для сток-режиму), а не замінювати ядро.
