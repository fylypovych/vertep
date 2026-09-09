# Повний аудит і план завершення концепції Vertep

Дата аудиту: 2026-09-09  
Базовий commit: `4dc632c9` (`0.0.1.25`)  
Стан: аудит враховує також незакомічені зміни робочого дерева, зокрема Angular Setup Wizard і storyboard flow.

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

Vertep має працездатний фундамент оркестратора: Job persistence, stages/scenes, task queue, leases, retries, dead-letter, Worker heartbeat, capability/VRAM-aware dispatch, Node Registry, provider interfaces, artifacts, базовий pipeline, live publisher adapters, update trust і release contract. Web UI V2 уже покриває основні сторінки та збирається.

Концепція ще не працює як завершена Zero Shell production-система. Основні причини:

1. Немає одного перевіреного наскрізного acceptance flow від First Run до live publication і recovery.
2. Storyboard реалізований у backend/Telegram, але відсутній у Web UI V2; LLM-виклик виконується в CORE executor, що суперечить цільовій вимозі виконувати генерацію на Text Worker.
3. Ролі описані й мають executors, але повний deployment кожної ролі та production image qualification не доведені.
4. Publisher API adapters існують, однак OAuth, credential readiness, test publish і керування platform-specific channel metadata через UI неповні.
5. Alerts обчислюються на запит і не мають persistent lifecycle/acknowledgment.
6. Settings перевантажений одним компонентом; domain API clients завершені лише частково.
7. Поточний Browser E2E: `27 passed, 3 failed`; файл також містить помилково вкладений ручний runner, який рекурсивно запускає тести.
8. Completion plan має завищені `DONE`: V2C-802/803 не можуть бути DONE без реального acceptance, а V2C-801 не DONE при наявних падіннях.

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

### Фаза A — стабілізувати поточну гілку

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-A01 | Розділити незакомічені зміни за scope і завершити integration | IN_PROGRESS | Storyboard, Setup і Web UI changes проходять build/tests; жоден файл не втрачено |
| VC-A02 | Виправити Browser E2E infrastructure | TODO | Прибрати вкладений ручний runner із test function; один запуск не рекурсивний |
| VC-A03 | Виправити Workers render/token contracts | TODO | `test_workers_page_loads` і `test_worker_wizard_opens_and_shows_token` зелені без timeout/workaround |
| VC-A04 | Відновити contract-test gate | TODO | Contract tests tracked, не ігноруються `.gitignore`, перевіряють frontend ↔ OpenAPI |
| VC-A05 | Закріпити baseline | TODO | `compileall`, весь pytest, Browser E2E, Angular build і `git diff --check` успішні |

### Фаза B — правильний distributed content pipeline

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-B01 | Формалізувати Storyboard API/state machine | IN_PROGRESS | Structured statuses, version conflicts, retry/error/event contracts і migration старих Job |
| VC-B02 | Перенести storyboard inference на Text Worker | TODO | CORE тільки створює task; `worker/role_executor.py:execute_text` повертає validated artifact; lease/cancel/retry працюють |
| VC-B03 | Crash recovery та idempotency storyboard | TODO | Restart у QUEUED/GENERATING без дублювання версії; Telegram update/callback idempotent |
| VC-B04 | Storyboard workspace у Job Detail V2 | TODO | Версії, scenes, duration, approve/reject/regenerate/revision, stale conflict і errors доступні у UI |
| VC-B05 | Telegram storyboard acceptance | TODO | Fake Telegram + fake Ollama/Text Worker: topic → version 1 → revision → version 2 → approve → SCRIPT_READY |
| VC-B06 | Pipeline stage contract | TODO | Storyboard approval запускає TTS/assets; жодної повторної script generation; PAUSE/CANCEL/RETRY узгоджені |

### Фаза C — Worker fleet і ролі

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-C01 | Єдиний Node/Worker read model | TODO | Fleet не губить registered offline nodes і heartbeat metrics; documented state mapping |
| VC-C02 | Завершити onboarding wizard | TODO | Token TTL → enrollment → certificate → self-test → READY показано одним UI flow |
| VC-C03 | Завершити remote controls | TODO | Drain/resume/quarantine/self-test/disable/restart/revoke/renew виконуються на реальному test node |
| VC-C04 | Text Node production | TODO | Ollama model placement/pull progress/cache/streaming/cancel/version lifecycle |
| VC-C05 | Voice Node production | TODO | TTS runtime image, voice catalog, preview і pipeline audio artifact |
| VC-C06 | GPU Node production | TODO | NVIDIA та AMD profiles, real ComfyUI workflow, cancel, multi-scene artifacts |
| VC-C07 | Publisher/Backup/Monitoring nodes | TODO | Кожна роль має image, health/self-test, task execution, update і failure recovery |
| VC-C08 | Seven-role deployment matrix | TODO | Clean Ubuntu VM acceptance для core/gpu/text/voice/publisher/backup/monitoring |

### Фаза D — publishing і content operations

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-D01 | Platform channel schemas | TODO | Typed metadata для Telegram/YouTube/TikTok/Facebook/Instagram/Threads, validation backend+UI |
| VC-D02 | OAuth/credential lifecycle | TODO | Connect/refresh/revoke/status flows, секрети не повертаються у UI |
| VC-D03 | Test publish/readiness | TODO | Channel readiness і explicit test action без hardcoded success |
| VC-D04 | Durable publication attempts | TODO | Attempts, external ID/URL, error, retry, idempotency key зберігаються структуровано |
| VC-D05 | Published query | TODO | Server filters/pagination за brand/channel/status/date; granular retry failed target |
| VC-D06 | Live platform acceptance | TODO | Мінімум Telegram + один video platform у sandbox; mock не є доказом production readiness |

### Фаза E — Zero Shell operations

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-E01 | Розділити Settings на production routes | TODO | `/telegram`, `/publishing`, `/settings/secrets`, `/settings/roles`, `/settings/update`, `/settings/backups`; lazy loading |
| VC-E02 | Завершити domain API clients/types | TODO | Queue/Publishing/Operations/Setup/Storyboard clients; без `any`/необґрунтованого `unknown as` |
| VC-E03 | Persistent alerts | TODO | Alert entity, severity/source/link, ack/resolve, retention, SSE/poll cursor |
| VC-E04 | Monitoring integration | TODO | Real probes, Prometheus targets, Loki logs, Grafana dashboards і links з UI |
| VC-E05 | Secrets hardening | TODO | Key sealing, rotation audit, per-integration readiness і safe delete |
| VC-E06 | Backup/restore drill | TODO | Snapshot, manifest/checksum, restore concrete target, maintenance state, post-restore health |
| VC-E07 | Update Center completion | TODO | Readiness, node order, canary promote/rollback, logs/progress, immutable digests у UI |
| VC-E08 | Global policy/accessibility | TODO | Admin/viewer matrix, system-state guards, keyboard/focus, mobile, українська термінологія |

### Фаза F — acceptance і release qualification

| ID | Задача | Статус | Результат/acceptance |
|---|---|---|---|
| VC-F01 | Core First Run acceptance | TODO | Clean install → Angular setup → login → dashboard без shell |
| VC-F02 | Non-Core acceptance | TODO | Core + second node: token/PKI/enrollment/self-test/update/revoke |
| VC-F03 | Content factory acceptance | TODO | Telegram → Text Worker storyboard → approval → GPU/TTS → assembly → publish receipt |
| VC-F04 | Failure/recovery acceptance | TODO | Worker loss, lease expiry, retry/dead-letter, backup restore, update rollback |
| VC-F05 | Security acceptance | TODO | CSRF/RBAC, secret non-disclosure, revoked token/certificate, signed update rejection |
| VC-F06 | Release qualification | TODO | Один SHA; tests/build/images; signed manifest, SBOM, checksums, runtime tarball, tag/release/digests verified |

## 6. Рекомендований порядок delivery

1. **VC-A01—A05:** отримати чистий, відтворюваний baseline.
2. **VC-B01—B06:** завершити головний content flow і прибрати генерацію з CORE.
3. **VC-C01—C08:** зробити roles реальними deployment units.
4. **VC-D01—D06:** довести publishing до live operation.
5. **VC-E01—E08:** завершити Zero Shell administration.
6. **VC-F01—F06:** підтвердити концепцію наскрізними acceptance і release.

Не слід паралельно розширювати UI новими controls, доки для них немає стабільного backend contract і production operation. Перший цінний milestone — VC-A + VC-B: реальний Telegram storyboard flow через Text Worker. Другий — VC-C: production node fleet. Третій — VC-D + VC-E: publication і Zero Shell. Лише після VC-F Vertep можна вважати реалізованим у повній мірі.

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

## 8. Поточні перевірки аудиту

- Angular `npm run build`: успішно; є warning `NG8107` у `settings.component.ts:353`.
- Storyboard tests: `4 passed`.
- Backend suite без Browser E2E: `305 passed, 9 skipped` до додавання четвертого storyboard contract test; ціль наступного baseline — повторний повний запуск.
- Browser E2E: `27 passed, 3 failed`.
- Падіння: Workers table render, worker token `data-testid`, рекурсивний runner/SystemExit і подальша втрата локального server connection.
- `git diff --check`: успішно.
- Working tree не чистий; audit і plan не є release qualification.