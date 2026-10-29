# Architecture 2.0 — локальне приймання #367 / #368

Дата: 2026-10-29. Базовий commit: `a98e2de4a19a6906c9403e8495a37275baa8f445`, VERSION `0.0.2.87`. Реалізація перебуває в незакоміченому робочому дереві; цей SHA не оголошується SHA реалізованих змін у main. Відтворюваний склад локального source фіксується в `.pytest_tmp/issue-368-source.json` через SHA256 файлів і tracked diff. Commit, push, Release та зміни GitHub Issues не виконувалися.

Результат: **#367 IMPLEMENTED; #368 IMPLEMENTED локально в межах етапів 1–4**. Fingerprint охоплює сукупне робоче дерево з попередніми правками; він не приписує всі його зміни цьому Issue.

## Приймання

| Критерій | Реалізація | Перевірка / очікуваний результат |
| --- | --- | --- |
| #367 review та owner gate | [Версії шести контрактів, review і рішення власника](contracts-review-367.md) | Власник прямо погодив пакет та початок #368 у чаті 2026-10-29. Незмінні Git blob шести документів зафіксовано в review. Код #368/#369 та physical qualification не підміняють це рішення. |
| #368 AC1 | `core/workflow/{dsl,engine,state,database_state,runtime}.py`, `core/dispatcher.py`, `worker/protocol/transport.py` | `tests/test_workflow_dispatch_execution.py`: незалежні tasks працюють паралельно, залежна task споживає їхні фактичні outputs; retry має окремі спроби; timeout/cancel не приймає пізній output; pause/approval/conditional/revision зберігають стан. `tests/test_workflow_http_process.py` виконує DAG через HTTP та окремий Worker process із production `execute_task`/`execute_role_task`/`submit_result`. |
| #368 AC2 | `core/module/{manifest,registry,discovery,packages}.py`, `worker/module_runtime.py`, `scripts/vertep-cli.py`, `scripts/vertep`, `scripts/vertep.ps1` | `tests/test_architecture_contract_integration.py`: SemVer та legacy version, dependency ranges, cache replacement, підпис/недовірений signer/tamper, CLI install/info/list/uninstall через окремі процеси. Перевірено штатний Windows CLI wrapper. HTTP/process test виконує підписаний модуль на Worker; uninstall прибирає його з discovery й блокує наступний dispatch. |
| #368 AC3 | `core/protocol/{task,worker}.py`, `worker/protocol/handler.py`, `core/api/workflow_execution.py`, штатні heartbeat та Worker loop | HTTP/process tests перевіряють registration/assignment/claim/progress/result/renew/cancel, несумісну major version, неправильну lease і пізній result. Паралельні cancel/result запити не можуть підтвердити fenced output. Execution tests підтверджують одного owner, lease generation, worker-loss fallback та відхилення старої спроби. |
| #368 AC4 | `core/gpu/{provider_registry,router,backends,local_mode}.py`, `worker/gpu_routing.py`, `worker/role_executor.py` | `tests/test_workflow_provider_http.py`: LocalModel викликає налаштований executor; Ollama/OpenAI/ComfyUI/Lab реально звертаються до контрольованих HTTP endpoints. Lab використовує чинний `LeaseManager`/`VertepResourceClient`. Інтеграційні тести перевіряють priority/fallback, allowlist, local-only та uncertain submission. Вичерпання дозволених providers стає явною помилкою task, доступною в API та Telegram. |
| #368 AC5 | `core/telegram/workflows.py`, інтеграція з handlers, `core/api/jobs.py`, `core/pipeline.py` | `tests/test_workflow_telegram_protocol.py`: вибір налаштованого шаблону перед запуском, idempotency, pause/revision/re-execution та відхилення approval старої revision. Legacy HTTP/process test зберігає штатний generation pipeline і script approval; його workflow складається з однієї task, що відображає фактичний legacy job без повторного dispatch. |

Фактичні результати фінального набору та ширшої регресії наведено в локальних JUnit/log артефактах:

- `.pytest_tmp/issue-368-final-acceptance.xml`, `.pytest_tmp/issue-368-final-acceptance.log` — **77 passed, 0 skipped**, 51.01 s: усі цільові контракти, execution, CLI, Telegram, HTTP/process, Redis та PostgreSQL. Наведені вище критерії — PASS у цьому наборі; owner gate підтверджений окремим рішенням у чаті.
- `.pytest_tmp/issue-368-regression.xml`, `.pytest_tmp/issue-368-regression.log` — ширша API/role runtime/provider switch/queue/Telegram регресія: **245 passed, 2 skipped**. Skip стосуються POSIX directory fsync на Windows і PKI test без OpenSSL у PATH того запуску; вони не зараховуються як PASS.

Середовище: Windows, Python 3.10.11; справжні disposable Redis і PostgreSQL. Inference endpoints у тестах контрольовані; live OpenAI account, фізичний GPU та production deployment не використовувалися. Physical migration/retirement залишається в #326; module migration та checkpoint execution мають власника #369.

`compileall` і `git diff --check` — PASS; `scripts/vertep` пройшов `bash -n`. PostgreSQL workflow test додано до чинного CI job із disposable PostgreSQL service. Remote CI цими локальними перевірками не підміняється.

## Налаштування runtime

`WORKFLOW_DEFINITIONS_ROOT` указує на каталог затверджених YAML/JSON definitions. Кожен файл задає один шаблон; ім'я файла без extension є його id. За наявності каталогу Telegram пропонує шаблон після вибору персонажа й до генерації. Новий DAG через API створюється чинним `POST /api/jobs` з `workflow_template`; legacy запити зберігають попередню поведінку.

Мінімальний повний definition:

```yaml
name: Стандартний
version: 1.0.0
description: Генерація сценарію на Text Worker
tasks:
  - id: script
    type: text_generation
    input: {}
    output:
      script: json
    dependencies: []
    timeout_minutes: 30
    retry:
      max_attempts: 3
      delay_seconds: 5
    approval_required: true
```

Для умовної task `when: {param: extended, equals: true}` порівнює параметр job без eval. Worker отримує input разом з `params` і `dependencies`, де кожна залежність містить її збережений output. Module task додатково задає `module: <name>`; dispatch перевіряє виявлену версію, capabilities та ресурси цього модуля.

`WORKFLOW_STATE_BACKEND` підтримує `file` (наявні checkpoints), `redis` і `postgres`. Для database backend потрібен `WORKFLOW_STATE_URL`; для file використовується `WORKFLOW_STATE_ROOT`. Job/tasks/outputs/approvals зберігаються одним атомарним документом. Після process restart перервані RUNNING attempts залишаються PAUSED до reconciliation; старий compute не оголошується завершеним або вільним лише через restart. Повний backup/restore/process-resume acceptance належить #369.

`GPU_PROVIDERS_CONFIG` на Worker указує на JSON із `local_only` і впорядкованими за `priority` елементами `providers`. Елемент містить `name`, один із типів `local_model`, `lab`, `ollama`, `comfyui`, `openai`, а також потрібні йому `endpoint`, `model`, `resources`; credentials задаються тільки через назву env var `key_env`. Реєстрація сама по собі не означає readiness. Без цієї конфігурації зберігається чинний generation backend. Після uncertain submission резервування зберігається до підтвердженого завершення/cleanup; сліпого fallback на інший inference немає.

OpenAI adapter використовує документований [Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create); модель та endpoint задає конфігурація. У перевірках використано тільки локальний контрольований endpoint.

## Зовнішні модулі

CLI та Worker мають використовувати ті самі `VERTEP_MODULE_ROOT` і `VERTEP_MODULE_TRUST_ROOT` (для контейнерів — відповідні спільні mounts). Defaults у source checkout: `data/modules` і `config/module-trust` від кореня проєкту. Trust root містить тільки публічні Ed25519 keys `<signer>.pem`; приватний signing key не копіюється в package чи repository.

Package є JSON з `manifest`, `files`, `signer`, `signature`. `files` зіставляє відносні імена з base64 вмістом; підпис покриває UTF-8 canonical JSON об'єкта `{manifest, files}` (`sort_keys=True`, separators `(',', ':')`, `ensure_ascii=False`). Невірний/відсутній підпис або недовірений signer не допускається до завантаження коду. Шляхи з traversal/absolute/drive segments відхиляються.

Entry point `module.py` має API `execute(task)`, `cancel(task_id)`, `health()` і `resource_query()`. Лише verified, сумісний і ready модуль рекламує capabilities. Uninstall відключає нові виклики й discovery; активний виклик утримує свій перевірений namespace до завершення. Підписаний package зберігається для audit/rollback без видалення файлів.

Команди: `vertep module install <package.json>`, `vertep module info <name>`, `vertep module list`, `vertep module uninstall <name>`. Python owner: `python scripts/vertep-cli.py module ...`; Windows wrapper: `scripts/vertep.ps1 module ...`.
