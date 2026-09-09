Now I have a complete picture. Let me compile the audit and create the implementation plan.

---

# ПОВНИЙ АУДИТ ПРОЄКТУ VERTEP

## 📊 Поточний стан

| Компонент | Статус | Деталі |
|-----------|--------|--------|
| **Backend API** | ✅ **Повністю реалізовано** | 100+ endpoints у `core/api/*.py` та `core/app.py` |
| **Angular Build** | ✅ **Проходить** | 473 KB bundle, 1 warning (NG8107) |
| **Python Unit Tests** | ✅ **306 passed, 9 skipped** | Browser E2E виключені (потрібен запущений сервер) |
| **Browser E2E** | ❌ **30 failed** | `ERR_CONNECTION_REFUSED` — сервер не запущений (дозволено по AGENTS.md §21) |
| **VERSION** | `0.0.1.25` | Останній коміт `4dc632c9` |
| **Frontend Routes** | ⚠️ **Частково** | 18/24 routes реалізовано (6 відсутні) |
| **TypeScript Types** | ⚠️ **Частково** | Багато `Record<string, unknown>` замість строгих інтерфейсів |

---

## 🔍 Порівняння планів

### WEB_UI_V2_IMPLEMENTATION_PLAN_UK.md (оригінальний) — **НЕАКТУАЛЬНИЙ**
Позначив як `DONE`: V2-001 → V2-405 (всі крім V2-501–V2-604).
**Проблема:** Це лише UI-скелети. Браузерні тести падають, бо AuthGuard блокує доступ. Немає повних mutation controls, permissions, system-state guards.

### WEB_UI_V2_COMPLETION_PLAN_UK.md (новий) — **РЕАЛІСТИЧНИЙ**
| Task | Статус плану | Фактичний стан |
|------|--------------|----------------|
| V2C-001 Logs | IN_PROGRESS | ✅ Готово |
| V2C-002 Settings test | DONE | ✅ Готово |
| V2C-003 Contract tests | TODO | ❌ Не зроблено |
| V2C-101–804 (всі інші) | TODO | ❌ Не зроблено |

---

## ❌ Відсутні Frontend Routes (згідно completion plan)

| Route | Призначення | Backend API |
|-------|-------------|-------------|
| `/publishing` | Brands, channels, Publisher readiness, test publish | `/api/brands`, `/api/channels/*` |
| `/telegram` | Bot setup, transport mode, chats, ingestion | `/api/telegram/*` |
| `/settings/secrets` | Masked Secrets Store | `/api/settings/secrets` |
| `/settings/roles` | Node Roles, deployment plan | `/api/system/roles` |
| `/settings/update` | Update, readiness, rolling, rollback | `/api/system/update/*` |
| `/settings/backups` | Snapshots, restore, retention | `/api/system/backups` |

---

## 🎯 TypeScript Typing Gaps (models.ts)

Використовують `Record<string, unknown>` замість строгих типів:
- `Character.voice/visual/generation/publishing`
- `Brand.metadata/publishing`
- `Channel.metadata`
- `SystemStatus.providers/ollama/update`
- `ModelInfo.details`
- `BackupInfo` / `BackupSnapshot` — дублювання
- `WorkflowDocument` — тільки nodes, немає edges
- `IntegrationStatus` — неполний

---

## 🔐 Permissions / System-State Policy

**Відсутнє централізовано:**
- `viewer` vs `admin` UI guards
- `MAINTENANCE`/`UPDATING`/`EMERGENCY`/`RECOVERING`/`READ_ONLY` action blocking
- Disabled controls з причиною (tooltip)

Backend має middleware (`AdminAuthMiddleware`), але frontend не синхронізований.

---

## 📋 ПЛАН РЕАЛІЗАЦІЇ (пріоритетний порядок)

### Фаза 1: Фундамент (V2C-003, V2C-101–104) — **Тиждень 1–2**
| Task | Дії |
|------|-----|
| **V2C-003** Contract tests | Покрити `tests/test_web_ui_contracts.py` усі API: Jobs, Workers, Queue, Characters, Workflows, Brands/Channels, Operations, System, Setup. Перевірити payload names, enum states, response mappings. |
| **V2C-101** Domain API clients | Розділити `VertepApiService` на: `JobsApi`, `WorkersApi`, `QueueApi`, `ResourcesApi`, `PublishingApi`, `OperationsApi`, `SystemApi`, `SetupApi`. Тимчасовий facade для міграції. |
| **V2C-102** Повна типізація | Замінити всі `Record<string, unknown>` на строгі інтерфейси в `models.ts`. Додати: `RuntimeMetrics`, `SelfTestResult`, `WorkflowDocument` (повний), `IntegrationStatus` (повний), `InstallationManifest`, `CertificateStatus`, `SecurityCheck`, `UpdateOperation`, `BackupSnapshot`, Telegram bot info. |
| **V2C-103** Shared state primitives | Створити reusable: `RemoteData<T>` (loading/error/data), `MutationState` (idle/running/succeeded/failed), `PaginatedTable`, `ConfirmDialog`, `ToastService` — вже є, перевірити консистентність. |
| **V2C-104** Permissions/State policy | `PermissionService` + `AuthDirective` + `SystemStateGuard`. Матриця: role × operation × systemState. Disabled control показує tooltip з причиною. |

---

### Фаза 2: Job Lifecycle (V2C-201–205) — **Тиждень 2–3**
| Task | Backend | Frontend |
|------|---------|----------|
| **V2C-201** Job editor | `PATCH /api/jobs/{id}` з `expected_version` ✅ | Structured form: topic, character, priority, workflow, script, scene prompts. 409 conflict UI з reload/reapply. |
| **V2C-202** Timeline | Потрібен структурований event model | Timeline з stages/scenes/events, timestamps, worker assignment, links до artifacts/workers. |
| **V2C-203** Artifacts | `GET/POST /api/jobs/{id}/artifacts*` ✅ | Media preview, integrity verify, re-verify, download, reference/audio upload, invalid/corrupt state. |
| **V2C-204** Publish flow | `POST /api/jobs/{id}/publish` ✅ | Channel selection, approve-before-publish, progress polling, platform receipts/URLs, retry failed, partial-success. |
| **V2C-205** Published query | Потрібен backend filter/pagination | Server-side filters для published jobs, не локальне фільтрування. |

---

### Фаза 3: Operations & Queue (V2C-301–304) — **Тиждень 3**
| Task | Backend | Frontend |
|------|---------|----------|
| **V2C-301** Queue page | `/api/tasks/queue`, `/api/tasks/dead-letter` ✅ | Scheduled/ready/inflight/dead-letter, pagination, retry conflicts, refresh. E2E для кожного стану. |
| **V2C-302** Alerts | `/api/alerts` ✅ | Job/Worker links, acknowledgment (backend mutation або read-only view). |
| **V2C-303** Logs | `/api/logs` ✅ (V2C-001) | Polling/SSE, pause live updates, filter persistence, Worker/Job links, exception details, DOM row limit. |
| **V2C-304** Health/monitoring | `/api/health*`, `/api/metrics` ✅ | Grafana URL, degraded states, E2E healthy/degraded/API error. |

---

### Фаза 4: Workers & Roles (V2C-401–404) — **Тиждень 3–4**
| Task | Backend | Frontend |
|------|---------|----------|
| **V2C-401** Worker onboarding | `/api/nodes/registration-tokens`, `/api/nodes/register` ✅ | Role → token (TTL) → Core URL/cert → polling `/api/nodes` → registration → self-test result. |
| **V2C-402** Worker detail/metrics | `GET /api/nodes/{id}` ✅ | GPU, VRAM total/free, temp, GPU/CPU load, RAM/disk, heartbeat age, current job/task, workflows, capabilities, runtime version, cert expiry, self-test. |
| **V2C-403** Worker controls | `POST /api/nodes/{id}/actions`, `/revoke`, `/renew` ✅ | Drain/resume/quarantine/self-test/disable/enable/restart/update/revoke. State guards. Command acknowledgment + timeout/error. Logs → `/logs?node_name=...`. |
| **V2C-404** Roles deployment | `GET/POST /api/system/roles` ✅ | Operation ID, executor progress, services, health result, rollback error. Не вважати checkbox успішним deployment. |

---

### Фаза 5: Resources & Publishing (V2C-501–504) — **Тиждень 4–5**
| Task | Backend | Frontend |
|------|---------|----------|
| **V2C-501** Character editor | CRUD `/api/characters` ✅ | Metadata, system_prompt, voice, visual, generation, workflow, retries, publishing. ID immutable. Schema validation. Unsaved-changes guard. |
| **V2C-502** Workflow editor | CRUD `/api/workflows` ✅ | Form/JSON modes, schema validation, usage graph (Character/Job refs), safe delete з 409 details. |
| **V2C-503** Channels CRUD | CRUD `/api/brands/{id}/channels`, `/api/channels/*` ✅ | Update/delete, enabled, platform metadata, target validation, credential readiness, test publish. Secrets не в metadata. |
| **V2C-504** Telegram | `/api/telegram/*` ✅ | Route `/telegram`: setup/status/bot-info, webhook/polling mode, allowed/admin chat IDs, connection test, diagnostics. End-to-end `/character` command test. |

---

### Фаза 6: Settings, Update, Recovery (V2C-601–605) — **Тиждень 5–6**
| Task | Backend | Frontend |
|------|---------|----------|
| **V2C-601** Secrets Store | `/api/settings/secrets` CRUD ✅ | Route `/settings/secrets`: platform grouping, configured/missing, write-only form, rotate, delete confirm. Не повертати секрет у response/DOM. |
| **V2C-602** Providers/Models | `/api/system/models*`, `/api/models/*` ✅ | Provider config via deployment flow, text model list/pull/delete, pull progress, voice list, synthesis preview. Unavailable/empty/API error states. |
| **V2C-603** Update Center | `/api/system/update/*` ✅ | Route `/settings/update`: check, readiness, install, controlled restart, rolling start/status/cancel, canary promote/rollback. Drain acknowledgments, active jobs, busy workers, operation log. Без `window.location.reload()`. |
| **V2C-604** Backup/Recovery | `/api/system/backups*` ✅ | Route `/settings/backups`: snapshot list/create/restore. Перед restore: конкретний snapshot, поточний system state, metadata, consequences. Progress + health verification. Recovery-to-NORMAL ≠ update rollback. |
| **V2C-605** General settings | `/api/integrations`, `/api/settings/logo`, `/api/system/certificates*`, `/api/security/check` ✅ | Integrations health, logo upload/delete, installation manifest, certificate status/renewal, security check. Tech errors без секретів. |

---

### Фаза 7: Setup Wizard & Zero Shell (V2C-701–704) — **Тиждень 6–7**
| Task | Backend | Frontend |
|------|---------|----------|
| **V2C-701** Core Setup Wizard | `/api/setup*` ✅ | Route `/setup` (до AuthGuard за setup token). Кроки: role, installation, admin, hardware, AI backend, health, manifest, done. Перенести з `web/setup.html` без втрати security checks. |
| **V2C-702** Non-Core enrollment | `/api/nodes/register` ✅ | Core URL, cert, registration token, CSR, credential persistence, enrollment diagnostics, поява вузла в Core. GPU/Text/Voice/Publisher/Backup/Monitoring roles. |
| **V2C-703** Navigation/Lazy routes | — | Цільові routes: `/publishing`, `/telegram`, `/operations` (Alerts/Logs/Health), `/settings/secrets`, `/settings/roles`, `/settings/update`, `/settings/backups`, `/setup`. Feature routes → `loadComponent`. Sidebar за permissions/capabilities. Видалити legacy nav після parity. |
| **V2C-704** Accessibility/UX | — | Keyboard nav, focus trap, labels, dialog semantics, contrast, mobile tables/forms, українська термінологія. |

---

### Фаза 8: Acceptance & Release (V2C-801–804) — **Тиждень 7–8**
| Task | Критерії |
|------|----------|
| **V2C-801** Feature Browser E2E | Кожна feature page: happy path, empty state, API error, critical mutation. Стабільні `data-testid`. Перевірка відсутності `pageerror`. |
| **V2C-802** Admin acceptance | Повний сценарій: setup Core → login → Worker onboarding → Character → Workflow → Brand/Channel → Job create/edit → lifecycle → approve → publish → receipt → update readiness → backup → recovery. |
| **V2C-803** Non-Core acceptance | Bootstrap/enrollment, heartbeat, metrics, task claim/result, drain/resume, self-test, update, restart, revoke для кожної role. |
| **V2C-804** Release qualification | Clean checkout + Docker build (amd64/arm64). Runtime bundle містить Web UI V2. Всі routes відкриваються через CORE fallback. |

---

## 🚀 Найближчі дії (негайно)

1. **V2C-003** — написати contract tests (`tests/test_web_ui_contracts.py`)
2. **V2C-101** — розділити `VertepApiService` на domain clients
3. **V2C-102** — замінити `Record<string, unknown>` на строгі інтерфейси в `models.ts`
4. Додати відсутні routes в `app.routes.ts`: `/publishing`, `/telegram`, `/settings/secrets`, `/settings/roles`, `/settings/update`, `/settings/backups`
5. Створити компоненти для нових routes (skeleton з loading/error/empty states)

---

## ✅ Definition of Done (з completion plan)

План завершено лише після:
```bash
python -m compileall -q core adapters worker scripts installer tests
python -m pytest -q
python -m pytest tests/test_browser_e2e.py -q
npm run build
git diff --check
```

**Готовий починати з V2C-003 (contract tests) або V2C-101 (domain API clients)?**