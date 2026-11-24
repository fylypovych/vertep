Module Contract

> Статус: контракт; реалізація — `core/module/manifest.py`, `core/module/instances.py`, `core/module/registry.py`, `core/module/trust.py`, `core/module/gateway.py`, `core/module/lifecycle.py`, `core/module/data.py`, `core/module/service.py`, `worker/application_container.py`; автоматизовані перевірки — `tests/test_module_contract.py`, `tests/test_module_trust_isolation.py`, `tests/test_module_ui_browser_e2e.py`, `tests/test_module_lifecycle.py`, `tests/test_demo_module_e2e.py`, `tests/test_demo_module_negative.py`, `tests/test_role_module_migration.py`, `tests/test_role_module_routing.py` | Аудиторія: розробник, ревізор | Канон: [AGENTS.md](../../../AGENTS.md).

===============

Модель **VERTEP → ВУЗОЛ → МОДУЛЬ** ([#487](https://github.com/fylypovych/vertep/issues/487), реалізація [#492](https://github.com/fylypovych/vertep/issues/492)). Одна користувацька система модулів; `execution` та `application` — внутрішні контракти, а не дві системи керування. Worker і capabilities залишаються внутрішніми.

1. Три окремі ідентичності
---------------------------------
- **Вузол** — `node_id` зареєстрованого сервера/VM. Профіль вузла задає адміністратор: `environment` (`production` | `nonproduction`) та `isolation` (`host` | `vm`). Типово — `production`/`host`.
- **Визначення модуля** — `module_id` + `version` з manifest.
- **Екземпляр** — визначення, встановлене на конкретному вузлі: ключ (`node_id`, `module_id`). Звіт одного вузла замінює лише його власні екземпляри; екземпляри інших вузлів не затираються. Один `module_id` двічі на одному вузлі відхиляється.

2. Manifest і schema negotiation
---------------------------------
- Без `schema_version` manifest читається як legacy **1.0**: `name`, `version`, `capabilities`, `dependencies`, `resource_requirements`, `description`. Також читаються bundled `modules/*/manifest.yaml` (`depends_on`, `resources`). Серіалізація legacy manifest не змінює форму для чинних споживачів inventory/protocol.
- `schema_version: "2.x"` додає розширений контракт. Minor — forward compatible; непідтримуваний major відхиляється. Розширені поля в 1.x відхиляються.

```yaml
schema_version: "2.0"
name: brands
module_id: vertep.brands          # типово = name
version: "1.0.0"
kind: application                 # execution (типово) | application
core_compatibility: {min: "0.0.3.0", max: "0.0.9.99"}
dependencies:                     # модулі або системні core_runtime/python_runtime/docker_runtime/gpu_runtime
  - {name: characters, min_version: "1.0.0", max_version: "1.9.9"}
capabilities: [brands_api]
resource_requirements: {min_vram_mb: 0, min_cpu_cores: 1, min_ram_mb: 512}
placement:
  target: any                     # any | isolated_lab
  scaling: single_owner           # execution: replicated (типово); application: лише single_owner
  requires_gpu: false             # типово: min_vram_mb > 0
provides:                         # версійні API / events / extension points (MAJOR.MINOR)
  - {type: api, name: brands, version: "1.2"}
requires:
  - {type: event, name: characters.changed, min_version: "1.0", max_version: "1.9"}
api: {namespace: brands, version: "1.2", routes: ["/brands", "/brands/{id}"]}
ui: {entry: ui/index.js, navigation: [{id: brands, label: "Бренди"}]}
permissions: [brands.read, brands.write]
secret_refs:                      # лише посилання; значення секретів у manifest заборонені
  - {name: token, ref: "secret://brands/token"}
migrations: [{id: m001, version: "1.0.0"}]
services: [{name: api}]
health: {self_test: selftest, timeout_seconds: 30}
```

Додаткові поля 2.x ([#493](https://github.com/fylypovych/vertep/issues/493)):

```yaml
publisher: acme                                   # видавець = власник ключа підпису
provenance: {repository: github.com/acme/notes, revision: 3f2a9c1, forked_from: github.com/up/notes}
container: {image: "registry.example/notes@sha256:<64 hex>", port: 8080}   # обов'язково для application
ui:
  entry: index.html
  navigation: [{id: notes, label: "Нотатки"}]
  files: {index.html: "<sha256>"}                 # підписані digest файлів UI
```

- Меню панелі (рішення власника 23.11.2026) — лише з `ui.navigation` READY модулів (`GET /api/modules/navigation`, `core/module/navigation.py`). Поля пункту: `id`, `label`, необов'язкові `group` (`content`, `system`; інакше «Розширення модулів»), `admin_only`, `route`. Пункт веде на власний підписаний UI модуля (`/modules/<id>`). Вбудовану сторінку панелі (`route`) може задати лише офіційний модуль, якому CORE її відвів у `MODULE_PAGES`: characters → `/characters`, brands → `/brands`, publishing → `/published`. Поки модуль не READY, його пункту в меню немає, guard не відкриває сторінку, а API CORE цієї сторінки (`/api/characters*`, `/api/brands*`, `/api/channels*`, `POST /api/jobs/{id}/publish`) повертає 503 `module_is_not_active`. Нове завдання (`POST /api/jobs`, Telegram) створюється лише з READY модулями Characters і Brands; Telegram без них не пропонує вибору бренду чи персонажа. Ручна публікація `POST /api/jobs/{id}/publish` потребує READY модуля Publishing. Завдання, що вже виконуються, деактивація модуля не зупиняє (#499).
- `secret_refs` дозволені лише у власному scope: `secret://<module_id>/<name>`.
- `application` вимагає `container` з образом, закріпленим за `sha256` digest; `execution` контейнера не має.

Транзакційний lifecycle, migrations і архів — [#494](https://github.com/fylypovych/vertep/issues/494).

3. Readiness екземпляра
---------------------------------
Екземпляр `READY` лише коли одночасно:
- Worker повідомив `ready: true` після власного health/self-test модуля (встановлення саме по собі не є доказом);
- версія CORE входить у `core_compatibility`;
- placement дозволяє цей вузол (розділ 4);
- залежності на модулі задоволені READY екземплярами з сумісною версією, системні — версіями CORE; немає циклу;
- кожен `requires` задоволений READY модулем, що `provides` той самий тип/ім'я у діапазоні версій;
- немає конфлікту: різні модулі не можуть заявляти один `api.namespace` або той самий `provides` — обидва стають `NOT_READY` (fail closed);
- адміністратор погодив увесь задекларований набір `permissions` (розділ 8); для `application` Worker повідомив `endpoint` екземпляра.

Інакше `NOT_READY` з причинами (`self_test_not_passed`, `core_incompatible`, `placement_required`, `not_placed_on_node`, `placement_invalid`, `dependency_unmet:<name>`, `dependency_cycle`, `interface_unmet:<type>:<name>`, `interface_conflict`, `permission_approval_required`, `endpoint_missing`). Демотування транзитивне. Capabilities `NOT_READY` екземплярів CORE не оголошує; інші перевірені capabilities Worker зберігаються.

Локальний `ModuleRegistry` (встановлення пакета на вузлі) застосовує ті самі перевірки сумісності CORE, конфліктів і `requires`; видалення модуля, чий інтерфейс використовує інший модуль, відхиляється.

4. Placement і dispatch
---------------------------------
- Адміністратор задає placement: `PUT /api/nodes/module-registry/placement/{module_id}` `{"node_ids": [...]}`; профіль вузла — `PUT /api/nodes/module-registry/profile/{node_id}` `{"environment", "isolation"}`. Стан — `GET /api/nodes/module-registry`. Зміни — лише роль адміністратора. Placement зберігається атомарно у `<config_root>/module-placement.json` (`VERTEP_MODULE_PLACEMENT_FILE`).
- Сумісність: replicated `execution` модуль без заданого placement залишається доступним на вузлах, що його повідомляють (чинна поведінка). `application` та `isolated_lab` модулі без явного placement не READY.
- CORE для task із `module` формує цілі лише з READY placed екземплярів; Dispatcher обирає один вузол серед них, перевіряє ресурси конкретного екземпляра та фіксує `module_version` саме цього екземпляра. Перелік цілей Worker не отримує.
- Один task attempt має одного owner: lease/fencing Dispatcher (`owns_lease`, `accept_result`, `revoke_task`) без змін; повторний `tick` не видає вже зайнятий attempt іншому вузлу.

5. Stateful application: межі ownership
---------------------------------
- `application` завжди `single_owner`: placement рівно на один вузол; replication даних не визначена і тому заборонена.
- Перенесення placement на інший вузол відхиляється, поки екземпляр-власник READY. Спочатку модуль деактивується через lifecycle ([#494](https://github.com/fylypovych/vertep/issues/494)); автоматичного переносу live-сервісу чи даних немає. Перенесення даних/migrations між вузлами цей контракт не виконує.

6. LAB placement
---------------------------------
- LAB — модуль із `placement.target: isolated_lab`. Його placement дозволений лише на вузол із профілем `nonproduction` + `vm`; production-вузол відхиляється. Автоматичного розміщення немає.
- Вузол реєструється в наявному CORE звичайним enrollment; другий CORE і deployment-роль `lab` не створюються (значення `lab` для профілю відхиляється).
- Перекласифікація вузла на `production` робить LAB екземпляр `NOT_READY`.
- LAB state contract і заборона прямого доступу до production DB/Redis/socket/secrets — окремо в [#491](https://github.com/fylypovych/vertep/issues/491); цей контракт їх не вирішує.
- Профіль вузла — декларація адміністратора; реальний доказ окремої VM — у реальних перевірках ([#442](https://github.com/fylypovych/vertep/issues/442), [#491](https://github.com/fylypovych/vertep/issues/491)).

7. Контракт module API (виконання на Worker)
---------------------------------
Підписаний пакет надає `execute(task)`, `cancel(task_id)`, `health()` → `{"ready": bool}`, `resource_query()` → `dict` (`worker/module_runtime.py`). Inventory Worker передає manifest + `ready` у heartbeat (`module_inventory`); формат protocol `1.x` незмінний.

8. Довіра, provenance і погодження прав
---------------------------------
- Підпис Ed25519 покриває точний manifest і кожен байт файлів пакета (`core/module/packages.py`); змінений вміст або manifest відхиляється.
- Trust root (`config/module-trust` / `VERTEP_MODULE_TRUST_ROOT`): `publishers.json` — `{"<publisher>": {"keys": {"<key_id>": "<PEM>"}, "repositories": [...]}}`. Пакет 2.x приймається лише коли ключ підпису належить `manifest.publisher` (issuer) і `provenance.repository` входить у дозволені репозиторії цього видавця. Старі `<key_id>.pem` перевіряють лише legacy-пакети 1.x.
- Fork — інший репозиторій: довіра upstream не успадковується, навіть якщо fork підписано ключем upstream. Fork довіряється лише через власний запис видавця, доданий адміністратором.
- Підписані digest `ui.files` перевіряються під час встановлення і повторно перед кожною видачею UI.
- Сумісність із CORE перевіряється до встановлення/активації.
- Погодження: `PUT /api/nodes/module-registry/approval/{module_id}` `{"permissions": [...]}` (лише адміністратор; лише задекларовані права). Новий або розширений набір прав робить екземпляр `NOT_READY` до нового погодження. Ролі доступу (admin/moderator) — не deployment-ролі й модульним lifecycle не змінюються.

9. Ізоляція application-коду
---------------------------------
- CORE не імпортує й не виконує код модулів; Worker відмовляє завантажити `application` у свій процес.
- Worker запускає контейнер (`worker/application_container.py`) з фіксованим набором параметрів: `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, користувач `65532`, `--pids-limit`, tmpfs `/tmp`, один власний named volume `vertep-mod-<id>-data:/data`, власна мережа `vertep-mod-<id>`, порт лише на `bind_address` вузла. Заборонені: Docker socket, `--privileged`, host namespaces, `--cap-add`, `--device`, bind mounts. У середовище потрапляють лише `VERTEP_MODULE_ID`, `VERTEP_CORE_URL`, `VERTEP_MODULE_TOKEN` — без DB/Redis/секретів платформи.
- Обмеження Docker: опублікований порт контейнера досяжний з інших bridge-мереж того самого хоста. Мережа не є межею довіри: модуль має приймати лише запити з власним `x-vertep-module-token` (дорівнює `VERTEP_MODULE_TOKEN`), який CORE gateway додає до кожного переданого запиту, а інші модулі його не мають.
- Worker отримує токен для запуску: `POST /api/modules/token` (автентифікація вузла; лише для розміщеного на цьому вузлі й погодженого модуля).

10. API gateway, extensions і secrets
---------------------------------
- `/api/modules/{namespace}/v{major}/{path}` → READY екземпляр модуля з цим `api.namespace`, лише задекларовані `routes` і лише major з `api.version`. Користувачі проходять автентифікацію/RBAC платформи (moderator — у межах своїх брендів: Gateway передає `x-vertep-brands`, право на зміну перевіряє модуль); модулю передаються `x-vertep-user`/`x-vertep-role`, cookies і `Authorization` не передаються.
- Модуль викликає інший модуль лише з власним токеном і лише API, задекларований у його `requires` з сумісною версією; інакше `403`. Деактивований/непогоджений модуль втрачає API, токен, secrets і UI.
- Secrets: адміністратор зберігає значення лише задекларованих посилань — `PUT /api/nodes/module-registry/secrets/{module_id}`; модуль читає `GET /api/modules/self/secrets` зі своїм токеном і отримує тільки власні посилання з зашифрованого сховища платформи.

11. UI extensions
---------------------------------
- Список READY розширень — `GET /api/modules/ui-extensions`; файли — `GET /api/modules/ui/{module_id}/{version}/{path}` лише для підписаних `ui.files`, з перевіркою sha256 перед видачею, CSP `sandbox allow-scripts` і `X-Frame-Options: SAMEORIGIN`. Файли UI — код без даних, як і статичний bundle панелі.
- Web UI показує розширення в `<iframe sandbox="allow-scripts">` (opaque origin): немає доступу до DOM панелі, cookies чи session. Відсутнє, вимкнене, змінене або недоступне розширення показує повідомлення лише у своєму розділі; навігація й інші розділи працюють. Канал даних (#501/#504): власної мережі розширення не має (CSP `default-src 'none'`). Воно надсилає панелі `postMessage({type: 'vertep-module-request', id, method, path, body})`; панель приймає повідомлення лише від свого iframe і лише для шляхів у межах `api_base` цього модуля (`/api/modules/{namespace}/v{major}/…`, без `..`, `//`, `#`), виконує запит із сесією користувача через Gateway і відповідає `{type: 'vertep-module-response', id, status, body}`. Будь-який інший шлях — 403 без запиту. Права, бренди й аудит вирішують Gateway і CORE, як для будь-якого запиту користувача; `api_base` повертає `GET /api/modules/ui-extensions`.
- Зовнішні ресурси в UI розширення заборонені тим самим CSP: дозволено лише вбудовані `<script>` і `<style>`, тож CDN-бібліотеки й навіть окремі файли модуля не завантажаться. Бібліотеки вкладаються в підписану сторінку під час збирання. Приклад — Monitoring: `modules/monitoring/ui-src/` (шаблон, Chart.js 4.5.1 з ліцензією MIT) → `node scripts/build-monitoring-ui.mjs` (Tailwind компілюється у вбудований CSS, оновлюється sha256 у `manifest.yaml`; `--check` перевіряє актуальність).

12. Audit і LAB
---------------------------------
- Кожен виклик gateway (дозволений, відхилений, збій), видача UI-файлів (включно з probe при відкритті сторінки), читання/запис secrets, видача токенів, погодження й placement записуються в `<config_root>/module-audit.jsonl` (`VERTEP_MODULE_AUDIT_FILE`) без значень secrets. Перегляд — `GET /api/nodes/module-registry/audit`, лише адміністратор.
- LAB (`isolated_lab`) використовує ту саму identity адміністратора Vertep через gateway, отримує лише власні secrets, не отримує DB/Redis/Docker socket/секретів платформи; усі його дії аудіюються. Окремі LAB-панелі, grants і state — [#491](https://github.com/fylypovych/vertep/issues/491).

13. Транзакційний lifecycle ([#494](https://github.com/fylypovych/vertep/issues/494))
---------------------------------
- Стани: `INSTALLED` → `ACTIVE` ⇄ `INACTIVE` → видалено; `FAILED` — відновлена версія не пройшла self-test. Лише `ACTIVE` екземпляр може бути READY (причина `lifecycle_<state>` інакше), тож deactivate/update/uninstall одразу зупиняють новий dispatch, API, UI, токени й secrets.
- `install` — підпис/trust/provenance, наявність підписаних migrations, залежності/інтерфейси/сумісність із CORE до будь-якої зміни. `activate` — погоджені permissions, підготовка даних, migrations, self-test (Worker на розміщеному вузлі повідомляє цю версію ready з чистими причинами) і лише тоді `ACTIVE`.
- `deactivate`/`update`/`uninstall` спершу знімають модуль з dispatch, потім чекають завершення активних задач модуля до `VERTEP_MODULE_DRAIN_SECONDS` і керовано відкликають решту (lease fencing Dispatcher). Активні залежні модулі блокують deactivate/uninstall; update перевіряє, що нова версія задовольняє всіх активних залежних.
- `update` — лише з явним `confirm: true` адміністратора; розширені permissions потребують нового погодження до update. Попередня версія, її пакет і знімок даних зберігаються; `rollback` повертає попередню версію разом зі знімком даних/конфігурації на момент перед update (зміни після update відкидаються).
- Admin API (лише адміністратор): `GET /api/nodes/module-registry/lifecycle`; `POST /api/nodes/module-registry/lifecycle/{install|activate|deactivate|update|rollback|uninstall}` з `package`/`module_id`/`confirm`.

14. Дані модуля
---------------------------------
- Модуль із `migrations` отримує в спільному PostgreSQL (`VERTEP_MODULE_DATABASE_URL` або `DATABASE_URL`; потрібне право CREATEROLE) схему `mod_<id>` і login-роль `mod_<id>` — власника лише цієї схеми, без CREATE у `public` і без доступу до інших схем. Пароль ролі — у зашифрованому сховищі платформи; модуль читає облікові дані `GET /api/modules/self/database` своїм токеном. Схема й роль позначаються ідентифікатором модуля-власника. Module ID, чиї schema/role збігаються з уже встановленим модулем (наприклад, `a-b` і `a_b` або довгі ID зі спільним обрізаним префіксом), відхиляються до provisioning; БД також відмовляє в `ensure`/`restore`/`drop` для чужої позначеної схеми або ролі (#493).
- Migrations — підписані файли пакета `migrations/<id>.sql`, порядок — `manifest.migrations`; виконуються від ролі модуля з `search_path` = його схема (без жорстко вписаної назви схеми), кожна в окремій транзакції, облік у `<schema>._vertep_migrations`. Помилка migration відкочує її повністю.
- Config і файли: `<lifecycle_root>/data/<module>/config` і `/files` (`VERTEP_MODULE_LIFECYCLE_ROOT`, типово `<config_root>/module-lifecycle`); їхній власник — модуль, вони входять у знімки й архів.
- LAB (`isolated_lab`) схеми в production PostgreSQL не отримує: активація модуля LAB з migrations відхиляється; його state визначає [#491](https://github.com/fylypovych/vertep/issues/491).

15. Журнал, rollback і відновлення після перезапуску
---------------------------------
- Формат журналу `vertep-module-journal/1`: `journal/<module>.json` з `op`, `phase` (`snapshot_taken` → `applied`; для uninstall `archiving` → `archive_verified`), `snapshot`, версіями й `was_active`. Журнал і стан записуються атомарно з `fsync` до наступної фази.
- Знімок `snapshots/<module>/<op>/`: запис модуля, export БД (DDL з migrations + дані `COPY`), копія config/files.
- Помилка install/migration/apply/self-test → відновлення знімка (БД + config + files + запис) і видалення журналу. Відновлена версія стає `ACTIVE` лише після власного self-test, інакше `FAILED` — без хибного READY.
- `recover()` при старті CORE (створення WorkflowRuntime): незавершений update/activate/rollback відкочується до знімка; uninstall після `archive_verified` завершується, раніше — скасовується (модуль `INACTIVE`, дані цілі). Повторний `recover()` нічого не робить.

16. Архів перед видаленням
---------------------------------
- `uninstall` вимагає `confirm: true`, створює архів `archives/<module>-<version>-<UTC>-<id>.tar` і перевіряє його: контрольні суми всіх файлів і реальне відновлення БД у тимчасову схему `vtmp_verify_<random>` поза простором імен `mod_` з порівнянням даних із живою схемою. Будь-яка помилка блокує видалення; модуль лишається `INACTIVE` з усіма даними. Після видалення CORE зберігає позначку `UNINSTALLED` (і після перезапуску), тому екземпляр, який вузол ще звітує як готовий, не отримує API/UI/dispatch доступу до повторного встановлення.
- Формат `vertep-module-archive/1` (звичайний tar, без шифрування): `archive.json` (версія формату, модуль, версія, `encrypted: false`, sha256 кожного файлу), `manifest.json`, `package.json` (підписаний пакет), `database.json`, `migrations/*.sql`, `config/*`, `files/*`. Дані Monitoring (історія метрик у схемі модуля) входять так само. Secrets платформи й пароль ролі в архів не потрапляють.
- Каталог архівів `0700`, файли `0600`; доступ — лише адміністратор: `GET /api/nodes/module-registry/archives`, `GET .../archives/{name}` (завантаження), `POST .../archives/{name}/restore` (відновлення в стан `INSTALLED`). Vertep архіви не видаляє — лише адміністратор вручну.
- Архів модуля не замінює зашифрований system backup і secrets: system backup/restore, локальне завантаження backup, окремий ключ #417, перевірка цілісності, post-restore health, checkpoint/references і fencing старих leases працюють без змін ([backup/restore](../../backup-restore.md)).

17. Спільні можливості для модулів і Worker-супервізор
---------------------------------
- Модуль виконує задачу на наявній спільній capability лише через CORE: `requires: [{type: capability, name: text_generation, min_version: "1.0"}]`, далі `POST /api/modules/self/tasks` `{"capability", "task_type", "input"}` своїм токеном і `GET /api/modules/self/tasks/{task_id}` (лише власні задачі). CORE створює звичайну workflow-задачу, Dispatcher віддає її готовому сумісному Worker з lease/fencing, результат перевіряється як будь-який workflow-результат.
- `GET /api/modules/self/database` повертає також адресу БД для контейнерів модулів (`VERTEP_MODULE_DATABASE_HOST`/`VERTEP_MODULE_DATABASE_PORT`, типово з DSN).
- Worker із `VERTEP_APPLICATION_MODULES=true` (`worker/application_supervisor.py`) на кожному heartbeat отримує `POST /api/modules/node-assignments` (розміщені на вузлі, погоджені, встановлені application-модулі), сам перевіряє підписи пакетів власним trust root, запускає/перезапускає контейнер потрібної версії з токеном від CORE, зупиняє непризначені контейнери й повідомляє екземпляр із `endpoint`; READY лише за відповіддю `/health`. Параметри: `VERTEP_APPLICATION_CORE_URL` (адреса CORE для контейнерів), `VERTEP_APPLICATION_BIND`, `VERTEP_NODE_ADDRESS`, `VERTEP_APPLICATION_PORT_BASE`, `VERTEP_APPLICATION_STATE`.

18. Демонстраційний модуль `demo-notes` ([#495](https://github.com/fylypovych/vertep/issues/495))
---------------------------------
Зразок для наступних міграцій, без бізнес-логіки й без GPU: `examples/modules/demo-notes/`.

- `app.py` — контейнерний застосунок: API `/notes` (лише з токеном від CORE gateway), власна таблиця `notes` у схемі `mod_demo_notes`, фонова служба `summarizer`, яка ставить задачу на спільну `text_generation` і зберігає перевірений результат; `/health`, підписаний `ui/index.html`.
- `migrations/m001_notes.sql` (1.0.0), `m002_reviewed.sql` (1.1.0), `Dockerfile` (користувач 65532).
- `build.py` — збирає образ, публікує його в registry, закріплює manifest за отриманим sha256 digest і підписує пакет Ed25519 (`--version 1.0.0|1.1.0 --registry --key --signer --publisher --repository --output`).

Автоматизоване приймання (disposable: Docker registry, контейнер модуля, PostgreSQL у контейнері, CORE на loopback, Worker-супервізор і штатний виконавець задач; LLM — локальна заглушка):

| Test ID | Що доводить |
| --- | --- |
| `tests/test_demo_module_e2e.py::test_demo_module_full_lifecycle` | install (підписаний пакет, образ за digest) → placement/погодження → запуск обмеженого контейнера Worker-ом → activate (схема, migration, self-test) → API/UI через gateway → фонова задача через CORE на Worker (`text_generation`) → deactivate → update 1.1.0 (новий migration, дані збережено) → rollback → uninstall з перевіреним архівом → restore з архіву → дані прочитано знову; audit |
| `tests/test_demo_module_negative.py` | відсутня залежність, несумісна версія CORE, немає погодження прав, немає готовності, невдалий self-test, не налаштована БД — без хибного READY; відсутній/невдалий модуль не блокує API й workflow платформи |

Реальні CPU-вузли, перезапуск і оновлення на стенді — [#442](https://github.com/fylypovych/vertep/issues/442); приклад не є GPU-кваліфікацією.

19. Чинні ролі під керуванням модулів ([#497](https://github.com/fylypovych/vertep/issues/497))
---------------------------------
Mapping за main 0.0.3.89 (`config/node_roles.json`, `worker/role_executor.py`, `worker/migration.py`):

| Роль / сервіси вузла | Модуль (bundled у релізі) | Задачі → capability | Залежності вузла (не модулі) | Стан до #497 → зроблено в #497 |
| --- | --- | --- | --- | --- |
| text: worker, ollama | `text` | text, script, storyboard → `text_generation` | ollama / LLM runtime | шлях роль→модуль лише через env-прапорець вузла → керування через lifecycle/placement |
| gpu: worker, comfyui | `image` | image, video → `image_generation`, `video_generation` | ComfyUI, CUDA/ROCm | те саме; GPU parity — [#441](https://github.com/fylypovych/vertep/issues/441) |
| voice: worker, tts | `audio` | voice → `speech_synthesis` | TTS runtime, моделі голосів | те саме |
| publisher: worker, publisher-worker | `publisher` | publish → `publishing` | адаптери `publishers/<platform>/` | модульного шляху не було → новий тонкий модуль над чинним виконавцем |
| backup: worker, backup-service | `backup` | backup → `backup` | backup/snapshot/archive сервіси | те саме; system backup/restore CORE не змінюється |
| gpu: `assembly` (FFmpeg) | — | — | — | власник [#498](https://github.com/fylypovych/vertep/issues/498), тут не переноситься |
| monitoring | — | — | — | власник [#503](https://github.com/fylypovych/vertep/issues/503) |
| core | — | — | PostgreSQL, Redis, proxy | CORE самодостатній, не модуль |

Повторно використано без змін: черга Redis і Job lifecycle, lease/fencing, cancel/stale fencing, self-test/readiness Worker, виконавці ролей, provider interfaces, shadow parity із replay зовнішніх ефектів (`worker/migration_trace.py`), workflow DAG/static-dynamic вибір/review/revision/checkpoint.

**Перемикання маршруту** (`core/module/routing.py`): у `_select_worker` і відповіді `POST /api/tasks/claim`.
- Функція без запису lifecycle або лише `INSTALLED` — legacy-шлях ролі без змін.
- `ACTIVE` — задачу забирає лише вузол із READY розміщеним екземпляром; задача отримує `module_name`, `module_version`, `module_routed: true`, Worker виконує той самий виконавець через модульний шлях (`worker/migration.py`), один attempt — один owner.
- `INACTIVE`/`FAILED`/під час update — жоден вузол не забирає задачу, вона чекає в черзі (без мовчазного повернення до legacy).
- `uninstall` повертає функцію на legacy-шлях. Bundled-модулі (`BUNDLED` у `core/module/lifecycle.py`) встановлюються `POST /api/nodes/module-registry/lifecycle/install` `{"bundled": "<модуль>"}` із manifest поточного релізу (довіра — підписаний реліз). Сам bundled-пакет не оновлюється окремим `update`; bundled-роль, для якої вийшов підписаний контейнерний пакет тієї самої версії компонента, переходить на нього через `update` (#511), а офіційні модулі з контейнером оновлюються підписаним пакетом окремо від CORE (AGENTS.md §2.21). `web-ui` — системний компонент: дії встановлення й активації для нього повертають 409.
- Worker завжди звітує bundled-модуль своєї ролі з `enabled` (env-прапорець). Без прапорця й без lifecycle екземпляр `NOT_READY` (`bundled_not_managed`) і не додає неперевірених capabilities.
- Обмеження: job-less voice preview (`voice_dispatch`) лишається на legacy-шляху.

**Compatibility guards** (автоматизовано): legacy-конфіг ролей і env-прапорці `VERTEP_<MODULE>_MODULE_ENABLED`/`_SHADOW` працюють як раніше; невідповідність версії модуля CORE і Worker не дає READY; перезапуск CORE зберігає стан lifecycle.

| Test ID | Доказ |
| --- | --- |
| `tests/test_role_module_migration.py::test_route_switch_parity_rollback_and_restart` | реальний контур CORE+Redis+виконавець Text із контрольованим Ollama: legacy → installed (без перемикання) → ACTIVE (claim із прив'язкою, модульний шлях, parity результату) → restart CORE → deactivate (задача чекає) → невідповідність версії (без READY) → uninstall (повернення legacy, задача, що чекала, виконана) |
| `tests/test_role_module_migration.py::test_routed_task_cancel_fences_a_stale_module_result` | скасування Job під час задачі на модульному маршруті: пізній результат відхиляється (409), черга порожня, Job `CANCELLED` |
| `tests/test_role_module_routing.py` | Image/Video, Voice, Publisher, Backup: bundled install/placement/activate/deactivate/uninstall і прив'язка за видом задачі; той самий виконавець через модульний шлях, один owner на attempt; shadow без другої публікації |

**Retirement (не виконувати без доказів [#441](https://github.com/fylypovych/vertep/issues/441)/[#442](https://github.com/fylypovych/vertep/issues/442) і прямого дозволу власника):** env-прапорці `VERTEP_TEXT|IMAGE|AUDIO|PUBLISHER|BACKUP_MODULE_ENABLED` і `_SHADOW`; legacy-шлях ролі в `worker/migration.py`/`execute_role_task`; рольові дані `modules`/`services` у `config/node_roles.json` як користувацький вибір; вибір ролі у Wizard/Web UI ([#496](https://github.com/fylypovych/vertep/issues/496)); поле `coexists_with` у bundled manifest.
