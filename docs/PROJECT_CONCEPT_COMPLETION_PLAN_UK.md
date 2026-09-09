# Повний аудит і план завершення концепції Vertep

Дата аудиту: 2026-09-09  
Базовий commit: `111e58b9` (`0.0.1.26`)  
Стан: аудит враховує реліз 0.0.1.26 з модулем сторібордів, lazy-loading маршрутів та Setup Wizard.

## 1. Призначення документа

Це актуальне джерело стану і подальшого виконання концепції Vertep. Документ зіставляє:

- правила і архітектуру з `AGENTS.md`;
- `docs/WEB_UI_V2_IMPLEMENTATION_PLAN_UK.md`;
- `docs/WEB_UI_V2_COMPLETION_PLAN_UK.md`;
- `docs/REQUIREMENTS_GAP_ANALYSIS_UK.md`;
- `docs/DEPLOYMENT_SECURITY_AUDIT.md`;
- `docs/architecture/open-source-audit.md`;
- ТЗ Telegram → Job → Ollama → versioned storyboard → approval;
- фактичні FastAPI routes, backend implementation, Worker executors, installer/runtime, Web UI V2 і тести.

Попередні позначки `DONE` не вважаються доказом. Функція готова лише тоді, коли існують production implementation, UI для штатної операції, коректний state/error flow і acceptance test без mock як доказу зовнішньої інтеграції.

Статуси аудиту:

- ✅ реалізовано;
- 🟡 частково реалізовано;
- ❌ відсутнє;
- ⚠️ backend є, але UI відсутній.

## 2. Виконавче резюме

Vertep має працездатний фундамент оркестратора: Job persistence, stages/scenes, task queue, leases, retries, dead-letter, Worker heartbeat, capability/VRAM-aware dispatch, Node Registry, provider interfaces, artifacts, pipeline, live publisher adapters, update trust, release contract, модуль сторібордів і Web UI V2 з lazy-loading. Після релізу 0.0.1.26 з'явився Angular Setup Wizard, сторіборди з REST API та Telegram approval flow.

**Концепція ще не працює як завершена Zero Shell production-система.** Основні причини:

1. **Критичне архітектурне порушення:** LLM-генерація (сценарії та сторіборди) виконується безпосередньо в CORE (`StoryboardService._ollama_client`, `providers.llm()` у `pipeline.prepare_job`). AGENTS.md §4.1 вимагає: «Всі генерації виконуються worker-ами через адаптери. CORE лише диспетчеризує задачі.» ТВЕКСЬТ Worker реально існує як сервіс, але storyboards і scripts генеруються in-process.
2. **TTS виконується в CORE pipeline** (`prepare_job` → `providers.tts().synthesize()`). Voice Worker є Docker-сервісом, але pipeline CORE викликає TTS напряму.
3. **Немає одного перевіреного наскрізного acceptance flow** від First Run до live publication і recovery.
4. **Storyboard відсутній у Web UI V2** — approval працює через Telegram inline keyboard та REST API, але не через Web UI.
5. **Publisher API adapters існують**, але OAuth, credential readiness, test publish і керування platform-specific channel metadata через UI неповні.
6. **Rolling update і backup/restore** мають backend, але не мають production acceptance.
7. **Немає multi-node deployment acceptance** — docker-compose показує всі сервіси на одній машині.

### Архітектурне порушення: генерація в CORE

Це головна перешкода для роботи концепції. Схема зараз:

```
CORE (prepare_job):
  providers.llm().generate_script()    ← LLM виклик в CORE
  storyboard._ollama_client()          ← LLM виклик в CORE
  providers.tts().synthesize()         ← TTS виклик в CORE
```

Цільова схема за AGENTS.md §4.1, §17, §19:

```
CORE (оркестратор):
  1. Створює task(type="text_generation", topic=..., character=...)
  2. Dispatcher шукає Text Worker з capability="text_generation"
  3. Worker claim task → execute_text() → result artifact
  4. CORE отримує artifact → продовжує pipeline
```

Те саме для TTS: CORE створює task → Voice Worker виконує → result повертається.

## 3. Матриця концепція ↔ backend ↔ Web UI V2

| Функціонал | Заплановано концепцією | Backend | Web UI V2 | Статус | Доказ і що бракує |
|---|---|---|---|---|---|
| Dashboard | KPI, system state, resources, fleet | `/api/status`, `/api/workers`, `/api/jobs`, `/api/metrics` | `/`, KPI/resources/workers | 🟡 частково реалізовано | `core/app.py`, `core/api/observability.py`, `web-v2/src/app/dashboard/dashboard.component.ts`; потрібні alerts/fleet health і acceptance стабільності render |
| Jobs list/create | Повна форма, scheduling, filters | `POST/GET /api/jobs` | `/jobs`, create form | 🟡 частково реалізовано | `core/api/jobs.py`, `jobs.component.ts`; базовий flow є, потрібні server-side filters/pagination і production validation усіх selector contracts |
| Job actions | Pause, Resume, Retry, Regenerate, Cancel, Delete, Approve, Publish | Routes існують | Job Detail має controls | 🟡 частково реалізовано | `core/api/jobs.py:130-257`, `job-detail.component.ts:667-928`; policy/state matrix і destructive consequences не покриті повним E2E |
| Job editor | topic, script, prompt, character, priority, workflow | `PATCH /api/jobs/{id}`, optimistic `expected_version` | editor є | 🟡 частково реалізовано | `core/models.py:JobUpdate`, `core/api/jobs.py:96`, `job-detail.component.ts`; потрібен scene-level editor зі schema validation і перевірений 409 conflict UX |
| Job stages/history/errors | Structured stages, attempts, events, errors | stages/scenes структуровані, events — рядки | timeline відображається | 🟡 частково реалізовано | `core/models.py`, `core/orchestration.py`, `job-detail.component.ts`; events треба перевести на structured model без parsing рядків |
| Artifacts | list/verify/download/upload/export/import/preview | Routes реалізовані | workspace є | 🟡 частково реалізовано | `core/api/jobs.py:270-401`, `core/artifacts.py`, `job-detail.component.ts:796-849`; потрібні media preview, limits UX і E2E integrity failure |
| Queue | ready/inflight/scheduled/dead-letter/retry | `/api/tasks/queue`, `/dead-letter`, retry | `/queue` | ✅ реалізовано | `core/queue.py`, `core/api/tasks.py`, `queue.component.ts`; production Redis acceptance лишається системним gate |
| Published | receipts, URLs, filters, retry | Job publication results, publish route | `/published` | 🟡 частково реалізовано | `core/api/jobs.py:215`, `published.component.ts`; немає окремого paginated query, повних platform URLs і granular retry contract |
| Alerts | Persistent lifecycle, severity, links, ack | Derived alerts без persistence/ack | `/alerts`, `/operations` | 🟡 частково реалізовано | `core/api/observability.py:113`, `alerts.component.ts`; потрібні Alert entity, acknowledgment, retention, SSE |
| Logs | filters, polling/SSE, node ingestion | list/filter та ingestion є | `/logs` | 🟡 частково реалізовано | `core/api/observability.py:67`, `logs.component.ts`; потрібні cursor pagination/SSE, retention і production Loki integration acceptance |
| Health/Monitoring | health history, metrics, Grafana | health, history, metrics, Prometheus | `/health` | 🟡 частково реалізовано | `core/api/observability.py`, `monitoring/`; потрібні real service probes, Grafana link/config і role deployment test |
| Workers/Fleet | list, metrics, health | heartbeat/list/health | `/workers` | 🟡 частково реалізовано | `core/api/workers.py`, `workers.component.ts`; Browser E2E `test_workers_page_loads` падає, fleet merge Node/Worker потребує єдиного contract |
| Worker metrics | GPU, VRAM, temp, load, CPU/RAM/disk/state | heartbeat model підтримує | detail/list показують частину | 🟡 частково реалізовано | `core/models.py:WorkerHeartbeat`, `worker/service.py`, `worker-detail.component.ts`; потрібні time series і missing/stale metrics semantics |
| Worker onboarding | role, token TTL, Core URL, registration/self-test | token/enroll/CSR/PKI routes є | wizard у `/workers` | 🟡 частково реалізовано | `core/api/nodes.py`, `core/node_registry.py`, `workers.component.ts`; `token-display` Browser E2E падає, немає повного очікування enrollment/self-test |
| Worker controls | drain/resume/quarantine/self-test/disable/restart/revoke/renew | actions/revoke/renew є | detail controls є | 🟡 частково реалізовано | `core/api/nodes.py:84-151`, `worker-detail.component.ts`; restart/logs та remote execution треба підтвердити на реальному node |
| Roles/Capabilities | Core/GPU/Text/Voice/Publisher/Backup/Monitoring | catalog, plan, executor є | Settings role selection | 🟡 частково реалізовано | `config/node_roles.json`, `core/deployment_plan.py`, `worker/role_executor.py`, `/api/system/roles`; потрібен production deploy/rollback кожної ролі |
| Characters | Конфіги, не код; повний editor | CRUD для всіх config sections | list + detail editor | 🟡 частково реалізовано | `core/configuration.py`, `core/api/resources.py`, character components; ID contract між двома create UI треба уніфікувати, потрібна schema/form validation замість raw JSON-only sections |
| Workflows | Registry окремо від CORE code | CRUD/validation | `/workflows` editor | 🟡 частково реалізовано | `core/workflows.py`, `core/api/workflows.py`, `workflows.component.ts`; потрібні usage references, versioning, safe delete і visual validation report |
| Brands | CRUD | CRUD є | `/brands` | ✅ реалізовано | `core/api/resources.py:72-108`, `brands.component.ts`; channel readiness рахується окремо |
| Channels | Modular publisher settings | CRUD/channel types | вкладено в Brands | 🟡 частково реалізовано | `core/api/resources.py:121-171`, `brand-channels.component.ts`; потрібні platform-specific schemas, credential status і test publish |
| Publisher modules | YouTube/TikTok/Facebook/Instagram/Threads/Telegram | live adapters є | лише channel config/publish | 🟡 частково реалізовано | `publishers/`, `adapters/publisher.py`; OAuth flows, hosted-video contract для IG/Threads, live sandbox acceptance відсутні |
| Telegram transport | Webhook/polling, access lists, ingestion | реалізовано в `core/app.py`, adapter/store | налаштування в Settings | 🟡 частково реалізовано | `/api/telegram/*`, `adapters/telegram.py`; дубль-заготовка `core/api/telegram.py` не підключена, `/brand` і `/character` command flow треба завершити |
| Storyboard через Ollama | versioning, retries, validation, Telegram approval | є в незакомічених `core/storyboard*.py`, REST routes | відсутній | ⚠️ backend є, але UI відсутній | `/api/jobs/{id}/storyboards*`; потрібні Text Worker dispatch, crash recovery, UI editor/history, API/Telegram E2E з fake Ollama |
| LLM providers/models | Ollama default + OpenAI-compatible, model operations | provider layer і model routes є | Settings list/pull/delete | 🟡 частково реалізовано | `adapters/llm_clients.py`, `core/api/models.py`, `/api/system/models`; pull progress/cancel, model placement per Text Node і backend switch deployment відсутні |
| TTS/Voice | Voice Node, models, preview | Piper/Kokoro provider layer та Voice executor; legacy fallback має placeholder | model/preview support неповний | 🟡 частково реалізовано | `adapters/providers/tts_backends.py`, `worker/role_executor.py`, `core/api/models.py`; потрібна гарантована runtime image, voice catalog UI і pipeline acceptance |
| Compute/Image/Video | GPU Worker + ComfyUI, replaceable providers | dispatcher/provider/video engines є | workflow/worker views | 🟡 частково реалізовано | `core/dispatcher.py`, `adapters/providers/compute_backends.py`, `video_engines.py`; потрібні production ComfyUI workflows/models, cancellation і artifact E2E |
| Secrets Store | write-only encrypted store, rotate/delete | API та AES-GCM persistence є | Settings masked CRUD | 🟡 частково реалізовано | `core/api/settings.py`, `core/first_run.py`, Settings; key sealing і OAuth-specific credential lifecycle не завершені |
| Update system | signed update, readiness, rolling/canary/rollback | значний backend готовий | Settings controls | 🟡 частково реалізовано | `core/update_*`, `core/rolling_update.py`, `/api/system/update*`; UI не покриває весь start/order flow, потрібен multi-node acceptance із immutable digest |
| Backup/Recovery | snapshot/create/restore/retention/recovery | Core proxy routes та Backup executor | Settings basic controls | 🟡 частково реалізовано | `/api/system/backups*`, `worker/role_executor.py`; потрібні backup-service production implementation validation, retention UI, restore drill і post-restore health proof |
| System resources/state | resources, NORMAL/maintenance/update/recovery | status/metrics/state machine | Dashboard/Settings/Health | 🟡 частково реалізовано | `core/system_state.py`, `/api/status`, `/api/system/recovery/normal`; потрібні complete action guards у всіх UI mutations |
| First Run Wizard Core | Zero Shell setup | setup routes є | Angular `/setup` є лише в незакомічених файлах | 🟡 частково реалізовано | `core/api/setup.py`, `web-v2/src/app/setup/`; потрібен commit, Browser E2E усіх кроків і replacement legacy setup proof |
| Non-Core enrollment | HTTPS Core, pinned certificate, token, credentials | backend flow є | Angular fields є | 🟡 частково реалізовано | `core/api/setup.py:first_run_complete`, Setup component; потрібен real two-node acceptance і secure credential persistence test |
| Authentication/permissions | Admin/viewer, CSRF, action visibility | session roles/CSRF middleware є | guard/policy є частково | 🟡 частково реалізовано | `core/security.py`, `core/app.py:AdminAuthMiddleware`, `auth.guard.ts`, `policy.service.ts`; потрібна route-level authorization matrix та viewer E2E |
| Deployment Wizard/runtime | Ubuntu bootstrap, Docker roles, Zero Shell | installer, role plan, system executor існують | setup/roles UI частково | 🟡 частково реалізовано | `installer/`, `core/deployment_plan.py`, `config/node_roles.json`; потрібна clean Ubuntu qualification для 7 ролей |
| Immutable runtime/release | signed manifest, SBOM, digests, bundle | scripts/workflow/contracts є | update UI частково | 🟡 частково реалізовано | `.github/workflows/`, `scripts/release-bundle.py`, release tests; version `0.0.1.25` має бути перевірена на повний GitHub Release artifacts окремо |

## 4. Перевірка старих планів

### `WEB_UI_V2_IMPLEMENTATION_PLAN_UK.md`

Початковий план корисний як target scope, але його таблиця прогресу застаріла. Tasks V2-501—V2-604 не можна переносити у DONE лише через появу controls у Settings або окремих routes. Особливо незавершені V2-502, V2-503, V2-504, V2-601, V2-602, V2-603 і V2-604.

### `WEB_UI_V2_COMPLETION_PLAN_UK.md`

Позначки V2C-801, V2C-802 і V2C-803 треба повернути у `IN_PROGRESS`: Browser E2E має 3 падіння, admin flow не проходить одним сценарієм, non-Core enrollment не перевірено на двох реальних nodes. V2C-101 не є повністю DONE: існують Jobs/Workers/Resources/System clients, але Queue/Publishing/Operations/Setup залишаються в монолітному `VertepApiService`. V2C-602 залишається TODO. V2C-804 залишається TODO.

### `REQUIREMENTS_GAP_ANALYSIS_UK.md` і deployment/security ТЗ

Попередні критичні прогалини зменшені: role executors, live publishers, provider layer, PKI/update trust і Angular setup з'явилися. Відкритими лишаються production qualification ролей, key sealing, OAuth lifecycle, complete health assertions, cache/model placement, cancellation/streaming Text Node та restore drill.

## 5. Єдиний подальший план реалізації

Статуси нового плану: `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`. `DONE` ставиться лише після виконання acceptance criteria задачі.

### Фаза 0 — Архітектурна корекція: виведення генерації з CORE (КРИТИЧНО)

Головна перешкода: CORE викликає LLM/TTS/ComfyUI напряму замість dispatch через Task Queue.

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-X01 | Storyboard LLM → Text Worker | TODO | `StoryboardService` створює task(type="text_generation"). Dispatcher → Text Worker. CORE не викликає Ollama. |
| VC-X02 | Script gen → Text Worker | TODO | `pipeline.prepare_job` створює task(type="script_generation"). Dispatcher → Text Worker → normalize_script. |
| VC-X03 | TTS → Voice Worker | TODO | `pipeline.prepare_job` створює task(type="speech_synthesis"). Voice Worker claim → synthesize → artifact. |
| VC-X04 | Verify GPU dispatch | TODO | Перевірити, що image/video використовує dispatch. Адаптувати якщо ні. |
| VC-X05 | Audit all providers.* calls | TODO | Видалити прямий LLM/TTS/Compute виклик з CORE. Тільки task dispatch + artifact. |

### Фаза A — Storyboard у Web UI V2

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-A01 | Storyboard viewer | TODO | Версія, title, description, hashtags, scenes. Loading/error/empty states. |
| VC-A02 | Approval/reject controls | TODO | Approve/Reject/Regenerate. Confirm dialog. E2E: generate → approve → SCRIPT_READY. |
| VC-A03 | Generate trigger | TODO | Кнопка у Job Detail. POST generate. Loading progress. |
| VC-A04 | Version history | TODO | Timeline: version, status, revision_request, decided_at/by. |
| VC-A05 | Crash recovery | TODO | Restart у QUEUED/GENERATING без дублювання версії. |

### Фаза B — Worker fleet і ролі

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-B01 | Єдиний Node/Worker model | TODO | Fleet не губить registered offline nodes; documented state mapping. |
| VC-B02 | Onboarding wizard | TODO | Token → enrollment → cert → self-test → READY одним UI flow. |
| VC-B03 | Remote controls | TODO | Drain/resume/quarantine/revoke/renew на реальному test node. |
| VC-B04 | Text Node production | TODO | Ollama model placement/pull/progress/cache/streaming/cancel. |
| VC-B05 | Voice Node production | TODO | TTS runtime image, voice catalog, preview, pipeline audio. |
| VC-B06 | GPU Node production | TODO | NVIDIA/AMD profiles, real ComfyUI workflow, cancel, multi-scene. |
| VC-B07 | Publisher/Backup/Monitoring | TODO | Кожна роль: image, health/self-test, task execution, update. |
| VC-B08 | Seven-role deployment | TODO | Clean Ubuntu VM: core/gpu/text/voice/publisher/backup/monitoring. |

### Фаза C — Publishing і content operations

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-C01 | Platform channel schemas | TODO | Typed metadata для кожної платформи, validation backend+UI. |
| VC-C02 | OAuth/credential lifecycle | TODO | Connect/refresh/revoke/status, секрети не повертаються у UI. |
| VC-C03 | Test publish/readiness | TODO | Channel readiness + test action без hardcoded success. |
| VC-C04 | Durable publication attempts | TODO | Attempts, external ID/URL, error, retry, idempotency key. |
| VC-C05 | Published query | TODO | Server filters/pagination за brand/channel/status/date. |
| VC-C06 | Live platform acceptance | TODO | Telegram + один video platform; mock не є proof. |

### Фаза D — Zero Shell operations

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-D01 | Settings production routes | TODO | `/telegram`, `/publishing`, `/secrets`, `/roles`, `/update`, `/backups`. |
| VC-D02 | Domain API clients/types | TODO | Queue/Publishing/Operations/Setup/Storyboard; без `any`. |
| VC-D03 | Persistent alerts | TODO | Alert entity, ack/resolve, retention, SSE. |
| VC-D04 | Monitoring integration | TODO | Real probes, Prometheus, Loki, Grafana з UI. |
| VC-D05 | Secrets hardening | TODO | Key sealing, rotation, per-integration readiness. |
| VC-D06 | Backup/restore drill | TODO | Snapshot → restore → post-restore health check. |
| VC-D07 | Update Center | TODO | Readiness, rolling, canary, immutable digests у UI. |
| VC-D08 | RBAC admin/viewer | TODO | Admin/viewer matrix, system-state guards. |

### Фаза E — Acceptance і release qualification

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-E01 | Core First Run acceptance | TODO | Clean install → Angular setup → login → dashboard без shell |
| VC-E02 | Non-Core acceptance | TODO | Core + second node: token/PKI/enrollment/self-test/update/revoke |
| VC-E03 | Content factory acceptance | TODO | Telegram → Text Worker storyboard → approval → GPU/TTS → assembly → publish receipt |
| VC-E04 | Failure/recovery acceptance | TODO | Worker loss, lease expiry, retry/dead-letter, backup restore, update rollback |
| VC-E05 | Security acceptance | TODO | CSRF/RBAC, secret non-disclosure, revoked token/cert, signed update rejection |
| VC-E06 | Release qualification | TODO | Один SHA; tests/build/images; signed manifest, SBOM, checksums, runtime tarball. |

## 6. Рекомендований порядок delivery

1. **Фаза 0 (VC-X01—X05):** вивести генерацію з CORE на Text/Voice Worker. Це основа для всіх наступних фаз.
2. **Фаза A (VC-A01—A05):** storyboards у Web UI V2 + crash recovery.
3. **Фаза B (VC-B01—B08):** production Node fleet з реальними images та roles.
4. **Фаза C (VC-C01—C06):** publishing до live operation.
5. **Фаза D (VC-D01—D08):** Zero Shell administration.
6. **Фаза E (VC-E01—E06):** наскрізні acceptance та release qualification.

**Перший milestone:** Фаза 0 + Фаза A — реальний content pipeline через Text Worker з Web UI storyboards.

**Другий milestone:** Фаза B — production node fleet.

**Третій milestone:** Фаза C + D — publication і Zero Shell.

Лише після Фази E Vertep можна вважати реалізованим у повній мірі.

## 7. Обов'язковий Definition of Done

Для кожної feature:

1. API schema і state transitions задокументовані та типізовані.
2. Backend виконує реальну операцію або чесно повертає `NOT_CONFIGURED`; mock/stub не є success path.
3. Штатна адміністративна операція доступна у Web UI V2.
4. Loading, empty, error, permission, conflict і mutation progress відображаються коректно.
5. Unit/contract tests перевіряють mapping та failure states.
6. Browser E2E перевіряє happy path і критичну помилку без `skip`, `xfail`, reload, timeout increase або `setTimeout` workaround.
7. Для зовнішньої інтеграції існує sandbox/production acceptance; fake transport використовується лише як детермінований contract test.
8. Для node/runtime feature є clean-machine або multi-node acceptance.
9. `python -m compileall -q core adapters worker scripts installer tests`, `python -m pytest -q`, Browser E2E, `npm run build` і `git diff --check` проходять.
10. Документація, configuration examples, migration/recovery і security implications оновлені.

## 8. Поточні перевірки аудиту (0.0.1.26)

- `compileall`: успішно, помилок немає.
- `pytest` без Browser E2E: `306 passed, 9 skipped`.
- Browser E2E: `30 failed` (всі `ERR_CONNECTION_REFUSED 127.0.0.1:8080` — немає запущеного сервера; допустимо за AGENTS.md §21).
- Storyboard unit-тести: `4 passed`.
- `git diff --check`: успішно.
- Working tree: чистий (commit `111e58b9` у `origin/main`).
- Angular build: потребує перевірки (warning `NG8107` у `settings.component.ts:353`).
